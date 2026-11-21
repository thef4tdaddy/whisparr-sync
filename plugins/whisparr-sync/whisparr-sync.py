#!/usr/bin/env python3
# =========================
# Imports
# =========================
import argparse
import fcntl
import hashlib
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import requests
import tomli
from config import (PluginConfig, load_config_logging, safe_json_preview,
                    switch_scene_log, truncate_path)
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      computed_field, field_validator)
from requests.adapters import HTTPAdapter
from stashapi import log as stash_log
from stashapi.stashapp import StashInterface
from urllib3.util.retry import Retry

logger: logging.Logger
# =========================
# Custom Exceptions
# =========================


class WhisparrError(Exception):
    pass


class SceneNotFoundError(WhisparrError):
    pass


class ManualImportError(WhisparrError):
    pass


class CoreReporter:
    """Persist reports before sending so retries retain the original event ID."""

    def __init__(self, config: PluginConfig):
        self.directory = config.STATE_DIRECTORY / "core-outbox"
        self.instance = config.SMUTWORKS_STASH_INSTANCE.strip() or "solo"
        self.endpoint = ""
        self.next_send = 0.0
        url = config.SMUTWORKS_CORE_URL.strip().rstrip("/")
        if not url:
            return
        try:
            parsed = urlsplit(url)
            token_file = os.getenv("SMUTWORKS_CORE_TOKEN_FILE")
            self.token = (
                Path(token_file).read_text() if token_file
                else os.getenv("SMUTWORKS_CORE_TOKEN", config.SMUTWORKS_CORE_TOKEN)
            ).strip()
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                    parsed.username is not None or parsed.query or parsed.fragment or
                    len(self.instance) > 200 or not self.token or not self.token.isascii() or
                    any(character.isspace() for character in self.token)):
                raise ValueError
            self.endpoint = url + "/v1/events"
        except (OSError, ValueError):
            logger.warning("SmutWorks Core reporting is disabled: check the Core URL, token and Stash instance")

    def emit(self, scene_id, transfer_id, kind, status, message):
        if not self.endpoint:
            return
        event_id = uuid4().hex
        event = {
            "event_id": event_id,
            "source": "whisparr-sync",
            "kind": kind,
            "status": status,
            "message": message[:500],
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "stash_instance": self.instance,
            "scene_id": str(scene_id),
        }
        if isinstance(transfer_id, str) and 0 < len(transfer_id) <= 200:
            event["transfer_id"] = transfer_id
        temporary = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as output:
                temporary = Path(output.name)
                json.dump(event, output)
            temporary.replace(self.directory / f"{time.time_ns()}-{event_id}.json")
        except (OSError, ValueError):
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            logger.warning("Could not save a SmutWorks Core report")
            return
        self.flush()

    def flush(self):
        if time.monotonic() < self.next_send:
            return
        try:
            with (self.directory / "send.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                with requests.Session() as session:
                    session.trust_env = False
                    deadline = time.monotonic() + 1.5
                    for path in sorted(self.directory.glob("*.json")):
                        if time.monotonic() >= deadline:
                            break
                        with session.post(
                            self.endpoint, data=path.read_bytes(),
                            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                            timeout=(0.5, 0.75), allow_redirects=False, stream=True,
                        ) as response:
                            if not 200 <= response.status_code < 300:
                                self.next_send = time.monotonic() + 30
                                logger.warning("SmutWorks Core returned HTTP %s; reports kept for retry", response.status_code)
                                break
                        path.unlink()
        except (OSError, ValueError):
            self.next_send = time.monotonic() + 30
            logger.warning("Could not send SmutWorks Core reports; queued reports kept for retry")


def report_health(config: PluginConfig) -> None:
    """Check Whisparr without starting a sync, then report to both destinations."""
    reporter = CoreReporter(config)
    kuma_url = os.getenv("SMUTWORKS_KUMA_PUSH_URL", "").strip()
    if not reporter.endpoint and not kuma_url:
        logger.info("No SmutWorks health destinations configured")
        return
    started = time.monotonic()
    with requests.Session() as session:
        session.trust_env = False
        try:
            with session.get(
                config.WHISPARR_URL.rstrip("/") + "/api/v3/system/status",
                headers={"X-Api-Key": config.WHISPARR_KEY}, timeout=(2, 3),
                allow_redirects=False, stream=True,
            ) as response:
                up = 200 <= response.status_code < 300
                message = "Whisparr API reachable" if up else f"Whisparr returned HTTP {response.status_code}"
        except (OSError, ValueError):
            up, message = False, "Whisparr API could not be reached"
        report = {
            "source": "whisparr-sync", "instance": reporter.instance,
            "status": "up" if up else "down", "message": message,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "interval_seconds": config.SMUTWORKS_HEALTH_INTERVAL,
        }
        if reporter.endpoint:
            try:
                with session.post(
                    reporter.endpoint.removesuffix("/events") + "/health", json=report,
                    headers={"Authorization": f"Bearer {reporter.token}"}, timeout=(2, 3),
                    allow_redirects=False, stream=True,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        logger.warning("Core heartbeat was rejected (HTTP %s)", response.status_code)
            except (OSError, ValueError):
                logger.warning("Could not send the heartbeat to Core")
        if kuma_url:
            try:
                parsed = urlsplit(kuma_url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
                    raise ValueError
                params = dict(parse_qsl(parsed.query, keep_blank_values=True))
                params.update(status=report["status"], msg=message, ping=report["latency_ms"])
                with session.get(
                    parsed._replace(query="", fragment="").geturl(), params=params,
                    timeout=(2, 3), allow_redirects=False,
                ) as response:
                    if not 200 <= response.status_code < 300 or response.json().get("ok") is not True:
                        logger.warning("Uptime Kuma did not accept the heartbeat")
            except (OSError, ValueError, AttributeError):
                logger.warning("Could not send the heartbeat to Uptime Kuma")


# =========================
# Helpers
# =========================
def load_from_toml(path: str) -> dict:
    p = Path(path).absolute()
    print(p)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomli.load(f)


def has_ignored_tag(scene: "StashSceneModel", ignore_tags: List) -> Optional[str]:
    for tag in scene.tags:
        if tag in ignore_tags:
            return tag
    return None


def wait_for_file(path: Path, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """
    Wait until the file exists on disk or timeout is reached.
    Returns True if file appeared, False if timeout reached.
    """
    start_time = time.monotonic()
    while not path.exists():
        if time.monotonic() - start_time > timeout:
            logger.warning(
                "File did not appear in target folder within %.1f seconds: %s",
                timeout,
                path,
            )
            return False
        time.sleep(interval)
    return True


def map_to_local_fs(path: Path, mappings: dict) -> Path:
    """
    Translate a server path into a local filesystem path using the mapping.
    Preserves full relative path including filename.
    Handles leading slashes and Windows/Unix normalization.
    """
    path_str = path.as_posix()  # keep forward slashes
    logger.debug("Original path: %s", path_str)

    for server_prefix, local_prefix in mappings.items():
        server_prefix = Path(server_prefix).as_posix().rstrip("/")
        local_prefix = Path(local_prefix).as_posix().rstrip("/")

        # Remove any trailing slash from path before matching
        if path_str.startswith(server_prefix + "/") or path_str == server_prefix:
            rel_path = path_str[len(server_prefix) :].lstrip("/")
            mapped_path = Path(local_prefix) / Path(rel_path)
            return mapped_path

    # No mapping applied, return original path
    return path


# =========================
# Pydantic Models
# =========================


class RetrievedModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BuiltModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileQuality(RetrievedModel):
    id: int
    name: str
    source: str
    resolution: int


class FileQualityWrapper(RetrievedModel):
    quality: Optional[FileQuality]


class ManualImportFile(BuiltModel):
    path: str
    movieId: int
    folderName: Optional[str] = None
    releaseGroup: str = ""
    languages: List[dict] = Field(
        default_factory=lambda: [{"id": 1, "name": "English"}]
    )
    indexerFlags: int = 0
    quality: Optional[FileQualityWrapper]


class Command(BuiltModel):
    name: str


class WhisparrSceneCreate(BuiltModel):
    title: str
    foreignId: str
    stashId: str
    monitored: bool
    qualityProfileId: int
    rootFolderPath: str
    addOptions: dict


class ManualImportParams(BuiltModel):
    folder: str
    movieId: int
    filterExistingFiles: bool = True


class ManualImportCommand(Command):
    name: str = "ManualImport"
    files: List[ManualImportFile]
    importMode: str = "auto"


class CommandResponse(RetrievedModel):
    id: int
    result: str = "unknown"
    status: str = "queued"


class RenameCommand(Command):
    name: str = "RenameFiles"
    movieId: int
    files: List[int] = Field(min_length=1)


class RefreshMovieCommand(Command):
    name: str = "RefreshMovie"
    movieIds: List[int]


class WhisparrStatistics(RetrievedModel):
    movieFileCount: int
    sizeOnDisk: int


class WhisparrScene(RetrievedModel):
    title: str
    id: int
    path: Path
    statistics: WhisparrStatistics
    foreignId: str

    @field_validator("path", mode="before")
    def convert_to_path(cls, v: Any) -> Optional[Path]:
        return Path(v) if v else None


class ManualImportPreviewFile(RetrievedModel):
    path: Path
    folderName: Optional[str] = None
    size: int
    quality: Optional[FileQualityWrapper]
    rejections: List[Any] = Field(default_factory=list)
    languages: List[dict] = Field(default_factory=lambda: [{"id": 0, "name": "Unknown"}])
    releaseGroup: str = ""
    indexerFlags: int = 0

    @field_validator("path", mode="before")
    def convert_path(cls, v: Any) -> Optional[Path]:
        return Path(v) if v else None


class StashFile(RetrievedModel):
    path: Optional[Path]

    @field_validator("path", mode="before")
    def to_path(cls, v: Any) -> Optional[Path]:
        return Path(v) if v else None


class StashSceneModel(RetrievedModel):
    title: str = ""
    tags: List[str] = Field(default_factory=list)
    files: List[StashFile] = Field(default_factory=list)
    stash_ids: List[Dict[str, str]] = Field(default_factory=list)
    # Default fallback
    stashdb_endpoint_substr: str = "stashdb.org"

    @field_validator("tags", mode="before")
    # Stash returns [] or list[{"name": str, ...}]
    def extract_tag_names(cls, v: Any) -> List[str]:
        if not v:
            return []
        if isinstance(v[0], dict) and "name" in v[0]:
            return [item["name"] for item in v]
        return v

    @computed_field
    def stashdb_id(self) -> Optional[str]:
        for sid in self.stash_ids:
            if self.stashdb_endpoint_substr in sid.get("endpoint", ""):
                return sid.get("stash_id")
        return None

    @computed_field
    def paths(self) -> List[Path]:
        return [f.path for f in self.files if f.path]


# =========================
# HTTP Helper
# =========================


def http_json(
    method: str,
    url: str,
    api_key: str,
    body: Optional[Union[BaseModel, dict]] = None,
    params: Optional[dict] = None,
    timeout: int = 30,
    response_model: Optional[Type[BaseModel]] = None,
    response_is_list: bool = False,
    dev: bool = False,
) -> Tuple[int, Union[BaseModel, List[BaseModel], dict, str]]:

    _session = requests.Session()
    _retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
    )
    _session.mount("http://", HTTPAdapter(max_retries=_retry))
    _session.mount("https://", HTTPAdapter(max_retries=_retry))

    if isinstance(body, BaseModel):
        body = body.model_dump(exclude_none=True, by_alias=True)

    headers = {"Accept": "application/json", "X-Api-Key": api_key}
    logger.debug(
        "%s %s params=%s body=%s", method, url, params, safe_json_preview(body)
    )

    try:
        r = _session.request(
            method, url, headers=headers, json=body, params=params, timeout=timeout
        )
        try:
            parsed = r.json()
        except ValueError:
            parsed = r.text

        if r.status_code >= 400:
            msg = f"HTTP {r.status_code} error for {method} {url}: {parsed}"
            logger.error(msg)
            raise WhisparrError(msg)

        if response_model:
            if dev:
                logger.debug("Attempting Parse to %s", response_model)
                logger.debug("raw data: %s", parsed)
            try:
                if response_is_list and isinstance(parsed, list):
                    return r.status_code, [response_model(**item) for item in parsed]
                elif not response_is_list and isinstance(parsed, dict):
                    return r.status_code, response_model(**parsed)
            except Exception as e:
                logger.exception("Failed to parse response into Pydantic model: %s", e)
                return r.status_code, parsed

        return r.status_code, parsed
    except requests.RequestException as e:
        logger.exception("HTTP request failed for %s %s", method, url)
        raise WhisparrError(f"HTTP request failed for {method} {url}: {e}") from e


# =========================
# Whisparr Interface
# =========================


class WhisparrInterface:
    def __init__(
        self,
        config: PluginConfig,
        stash_scene: StashSceneModel,
        http_func: Callable[..., Tuple[int, Any]] = http_json,
        report: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.stash_scene: StashSceneModel = stash_scene
        self.whisparr_scene: Optional[WhisparrScene] = None
        self.url: str = config.WHISPARR_URL.rstrip("/")
        self.key: str = config.WHISPARR_KEY
        self.monitored: bool = config.MONITORED
        self.move: bool = config.MOVE_FILES
        self.http_json = http_func
        self.rename: bool = config.WHISPARR_RENAME
        self.root_dir: str = str(config.ROOT_FOLDER or "")
        self.qualprofile: str = config.QUALITY_PROFILE
        self.config: Dict = config
        self.filenames: str = stash_scene.files
        self.report = report or (lambda kind, status, message: None)

    def process_scene(self) -> None:
        # Hooks can overlap. Keep the lock for the entire command lifecycle.
        if not self.stash_scene.stashdb_id or len(self.stash_scene.paths) != 1:
            raise WhisparrError("Exactly one source file and one StashDB ID are required; review multi-file scenes first")
        state_dir = self.config.STATE_DIRECTORY
        state_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f"{self.url}|{self.stash_scene.stashdb_id}".encode()).hexdigest()
        self.checkpoint = state_dir / f"{key}.json"
        with (state_dir / f"{key}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WhisparrError("This scene is already being processed") from exc
            if self.checkpoint.exists():
                pending = json.loads(self.checkpoint.read_text())
                if not pending.get("id"):
                    raise WhisparrError("Previous command outcome is unknown; inspect Whisparr before clearing checkpoint")
                self._wait_for_command(pending["id"])
                self.checkpoint.unlink()
            self.whisparr_scene = self.find_existing_scene()
            if not self.whisparr_scene:
                self.create_scene()
            if not isinstance(self.whisparr_scene, WhisparrScene):
                raise WhisparrError("Whisparr did not return a valid scene")
            existing = self._movie_files()
            if len(existing) > 1:
                raise WhisparrError("Multiple Whisparr files exist; refusing to choose or replace one")
            if not existing:
                self.import_stash_file()
                existing = self._movie_files()
                if len(existing) != 1:
                    raise ManualImportError("Import completed without exactly one registered movie file")
            else:
                logger.info("Whisparr already owns a file; skipping re-import and replacement")
                self.report("import.already_registered", "ok", "Whisparr already has this file")
            if self.rename:
                if self._rename_previews():
                    self._queue_command("RenameFiles")
                    if self._rename_previews():
                        raise WhisparrError("Rename completed but organization still differs from Whisparr naming settings")
                else:
                    logger.info("Whisparr naming is already correct; skipping rename command")
            final = self._movie_files()
            if len(final) != 1 or final[0]["id"] != existing[0]["id"] or not final[0].get("path"):
                raise WhisparrError("Unable to verify the final Whisparr file")
            # Path is authoritative from Whisparr, never reconstructed from the old filename.
            logger.info("Whisparr final file: id=%s path=%s", final[0]["id"], final[0]["path"])
            self.report("file.registered", "ok", f"Whisparr file {final[0]['id']}: {final[0]['path']}")

    def _rename_previews(self) -> List[dict]:
        status, previews = self.http_json(
            method="GET", url=f"{self.url}/api/v3/rename", api_key=self.key,
            params={"movieId": self.whisparr_scene.id},
        )
        if status != 200 or not isinstance(previews, list):
            raise WhisparrError("Unable to verify Whisparr naming preview")
        return previews

    def _movie_files(self) -> List[dict]:
        status, files = self.http_json(
            method="GET", url=f"{self.url}/api/v3/moviefile", api_key=self.key,
            params={"movieId": self.whisparr_scene.id},
        )
        if status != 200 or not isinstance(files, list):
            raise WhisparrError("Unable to retrieve registered Whisparr files")
        if any(not isinstance(f, dict) or f.get("movieId") != self.whisparr_scene.id
               or not isinstance(f.get("id"), int) or f["id"] <= 0 for f in files):
            raise WhisparrError("Unexpected movie-file ownership or ID")
        return files

    def _wait_for_command(self, command_id: int) -> None:
        deadline = time.monotonic() + self.config.COMMAND_TIMEOUT
        while True:
            status, command = self.http_json(
                method="GET", url=f"{self.url}/api/v3/command/{command_id}", api_key=self.key,
            )
            if status != 200 or not isinstance(command, dict):
                raise WhisparrError("Unable to read command status")
            state = str(command.get("status", "")).lower()
            if state == "completed":
                return
            if state in ("failed", "aborted", "cancelled", "canceled", "orphaned"):
                raise WhisparrError(f"Whisparr command {command_id} {state}; review before retry")
            if state not in ("queued", "started"):
                raise WhisparrError(f"Unknown command status: {state}")
            if time.monotonic() >= deadline:
                raise WhisparrError(f"Command {command_id} timed out; checkpoint retained for recovery")
            time.sleep(self.config.COMMAND_POLL_INTERVAL)

    def _submit_command(self, command: Command) -> None:
        # Write before POST: a lost response must never cause a blind repeat import.
        self.checkpoint.write_text(json.dumps({"name": command.name, "id": None}))
        status, response = self.http_json(
            method="POST", url=f"{self.url}/api/v3/command", api_key=self.key, body=command,
        )
        if status not in (200, 201) or not isinstance(response, dict) or not isinstance(response.get("id"), int):
            raise WhisparrError("Command submission outcome unknown; checkpoint retained")
        self.checkpoint.write_text(json.dumps({"name": command.name, "id": response["id"]}))
        stage = {"ManualImport": "import", "RenameFiles": "rename"}.get(command.name, "refresh")
        self.report(f"{stage}.accepted", "running", f"Whisparr accepted {command.name} command {response['id']}")
        self._wait_for_command(response["id"])
        self.checkpoint.unlink()
        self.report(f"{stage}.completed", "ok", f"Whisparr completed {command.name} command {response['id']}")

    def _source_path(self) -> Path:
        source = self.stash_scene.paths[0]
        # PATH_MAPPING maps Whisparr paths to paths visible in the Stash container.
        matches = []
        for remote, local in self.config.PATH_MAPPING.items():
            try:
                relative = source.relative_to(Path(local))
            except ValueError:
                continue
            matches.append((len(Path(local).parts), Path(remote) / relative))
        if not matches:
            return source
        matches.sort(key=lambda item: item[0], reverse=True)
        best = [path for length, path in matches if length == matches[0][0]]
        if len(set(best)) != 1:
            raise WhisparrError("Ambiguous source path mapping")
        return best[0]

    def find_existing_scene(self) -> Optional[WhisparrScene]:
        status, scenes = self.http_json(
            method="GET",
            url=f"{self.url}/api/v3/movie",
            api_key=self.key,
            params={"stashId": self.stash_scene.stashdb_id},
            response_model=WhisparrScene,
            response_is_list=True,
        )
        if status != 200 or not isinstance(scenes, list):
            raise WhisparrError("Scene lookup failed; refusing to create a potential duplicate")
        if not scenes:
            logger.info("No existing scenes found in Whisparr")
            return None
        if len(scenes) != 1:
            raise WhisparrError("Ambiguous Whisparr scene match; refusing to create another")
        if not isinstance(scenes[0], WhisparrScene) or scenes[0].foreignId != self.stash_scene.stashdb_id:
            raise WhisparrError("Whisparr returned a different StashDB identity")
        logger.info("Scene already exists in Whisparr: %s", scenes[0])
        return scenes[0]

    def create_scene(self) -> None:
        """
        Create a Whisparr scene based on the Stash scene.
        Guaranteed to have a stashdb_id at this point.
        """
        # Assert stashdb_id exists to satisfy mypy
        stashdb_id: str = self.stash_scene.stashdb_id  # type: ignore[assignment]

        scene_payload = WhisparrSceneCreate(
            title=self.stash_scene.title,
            foreignId=stashdb_id,
            stashId=stashdb_id,
            monitored=self.monitored,
            qualityProfileId=self.get_default_quality_profile(),
            rootFolderPath=self.get_default_root_folder(),
            addOptions={
                "monitor": "movieOnly" if self.monitored else "none",
                "searchForMovie": False,
            },
        )

        status, scene = self.http_json(
            method="POST",
            url=f"{self.url}/api/v3/movie",
            api_key=self.key,
            body=scene_payload,
            timeout=120,
            response_model=WhisparrScene,
        )

        if not isinstance(scene, WhisparrScene) or scene.foreignId != stashdb_id:
            raise WhisparrError("Invalid created scene identity; inspect Whisparr before retry")
        self.whisparr_scene = scene

        if status in (200, 201):
            logger.info("Added movie '%s' to Whisparr", self.stash_scene.title)
        else:
            msg = f"Failed to add movie '{self.stash_scene.title}': {scene}"
            logger.error(msg)
            raise WhisparrError(msg)

    def import_stash_file(self) -> None:
        source = self._source_path()
        if not self.move and not source.is_relative_to(self.whisparr_scene.path):
            raise ManualImportError("Enable MOVE_FILES to let Whisparr import and organize an external source")
        status, previews = self.http_json(
            method="GET", url=f"{self.url}/api/v3/manualimport", api_key=self.key,
            # movieId selects the destination-library scan and ignores folder.
            # Assign the intended movie only in the import command below.
            params={"folder": source.parent.as_posix(),
                    "filterExistingFiles": True},
            response_model=ManualImportPreviewFile, response_is_list=True,
        )
        if status != 200 or not isinstance(previews, list):
            raise ManualImportError("Manual import preview failed")
        matches = [p for p in previews if isinstance(p, ManualImportPreviewFile) and p.path == source]
        if len(matches) != 1:
            paths = [str(p.path) if isinstance(p, ManualImportPreviewFile) else "invalid preview record"
                     for p in previews]
            raise ManualImportError(
                f"Expected one full-path import match for {source}; preview paths: {paths}"
            )
        preview = matches[0]
        # Folder scanning only guesses identity from filenames. Explicitly assign
        # the verified StashDB movie and ask Whisparr to re-evaluate the preview,
        # just as selecting a movie in Interactive Import does.
        request = ManualImportFile(
            path=source.as_posix(), movieId=self.whisparr_scene.id,
            quality=preview.quality, languages=preview.languages,
            releaseGroup=preview.releaseGroup, indexerFlags=preview.indexerFlags,
        )
        status, reassigned = self.http_json(
            method="POST", url=f"{self.url}/api/v3/manualimport", api_key=self.key,
            body=[request.model_dump(mode="json", exclude_none=True, exclude={"folderName"})],
        )
        if status not in (200, 202) or not isinstance(reassigned, list) or len(reassigned) != 1:
            raise ManualImportError("Could not re-evaluate the file for the StashDB-matched scene")
        item = reassigned[0]
        if (not isinstance(item, dict) or item.get("path") != source.as_posix()
                or item.get("movieId") != self.whisparr_scene.id
                or not isinstance(item.get("movie"), dict)
                or item["movie"].get("id") != self.whisparr_scene.id
                or item["movie"].get("foreignId") != self.stash_scene.stashdb_id):
            raise ManualImportError("Re-evaluated preview did not confirm the intended StashDB identity and path")
        if not isinstance(item.get("rejections"), list):
            raise ManualImportError("Re-evaluated preview did not return valid rejection checks")
        preview = ManualImportPreviewFile.model_validate({**preview.model_dump(), **item})
        if preview.rejections:
            raise ManualImportError(f"Whisparr rejected the source: {preview.rejections}")
        self._submit_command(ManualImportCommand(
            importMode="move",
            files=[ManualImportFile(folderName=preview.folderName or None,
                                    path=source.as_posix(),
                                    movieId=self.whisparr_scene.id, quality=preview.quality,
                                    languages=preview.languages, releaseGroup=preview.releaseGroup,
                                    indexerFlags=preview.indexerFlags)],
        ))

    def _queue_command(self, commandname: str = "RefreshMovie") -> None:
        if commandname == "RenameFiles":
            files = self._movie_files()
            if len(files) != 1:
                raise WhisparrError("Rename requires exactly one owned file")
            command = RenameCommand(movieId=self.whisparr_scene.id, files=[files[0]["id"]])
        elif commandname == "RefreshMovie":
            command = RefreshMovieCommand(movieIds=[self.whisparr_scene.id])
        else:
            raise WhisparrError(f"Unsupported command: {commandname}")
        self._submit_command(command)

    def get_default_quality_profile(self) -> int:
        status, qps = self.http_json(
            method="GET", url=f"{self.url}/api/v3/qualityprofile", api_key=self.key
        )
        any_id = next(
            (item["id"] for item in qps if item["name"] == self.qualprofile), None
        )
        if status != 200 or any_id is None:
            raise WhisparrError(f"Configured quality profile not found: {self.qualprofile}")
        return int(any_id)

    def get_default_root_folder(self) -> str:
        result = self.http_json(
            method="GET", url=f"{self.url}/api/v3/rootfolder", api_key=self.key
        )
        rfs: List[Dict[str, str]] = result[1]

        # Check for configured root_dir
        if self.root_dir != "":
            rf = next((rf for rf in rfs if rf["path"] == self.root_dir), None)
            if rf is not None:
                return rf["path"]
            raise WhisparrError(f"Configured root folder not found: {self.root_dir}")

        # Fallback to first root folder if available
        if rfs:
            return rfs[0]["path"]

        # Safe fallback if list is empty
        raise ValueError("No root folders returned from API")


class StashHelpers:
    dev: bool = False
    STASH_DATA: Dict[str, Any] = {}
    _stash_conn = None
    toml_path: Path = None

    def __init__(self, scene_id: int):
        self.scene_id = scene_id

    @classmethod
    def open_conn(cls):
        """Lazy-load the stash connection."""
        if cls._stash_conn is None:
            try:
                cls._stash_conn = StashInterface(cls.STASH_DATA["server_connection"])
                logger.info("StashInterface connection established.")
            except KeyError:
                logger.error("Missing 'server_connection' in STASH_DATA.")
                return None
            except Exception as e:
                logger.exception("Failed to initialize StashInterface: %s", e)
                return None
        return cls._stash_conn


# =========================
# Main
# =========================
def preprocessor(dev: bool):
    global logger
    if dev:
        stash_log.info("\n\n***DEV_MODE***\n\n")
        toml_path = "../../dev.toml"
    else:
        toml_path = "/root/.stash/plugins/whisparr-sync/config.toml"
    STASH_DATA = {}
    if not dev:
        try:
            raw_data = sys.stdin.read()
            if not raw_data.strip():
                stash_log.error("No input data received from Stash hook.")
                return
            STASH_DATA = json.loads(raw_data)
            toml_path = str(Path(STASH_DATA.get("PluginDir") or Path(__file__).resolve().parent) / "config.toml")
        except Exception as e:
            print(f"Failed to parse input JSON: {e}")
            return
    else:
        dev_config = load_from_toml(toml_path)
        stash_log.info(f"TOML config:")
        stash_log.info(f"------------")
        stash_log.info(safe_json_preview(dev_config))
        stash_log.info(f"------------")
        STASH_DATA["server_connection"] = dev_config.get("STASH_CONFIG")
    StashHelpers.STASH_DATA = STASH_DATA

    try:
        logger, config = load_config_logging(
            toml_path=toml_path, STASH_DATA=STASH_DATA, dev=dev
        )
    except Exception as e:
        stash_log.error(f"Failed to load configuration: {e}")
        return
    return config


def process_single_scene(config, scene_id, transfer_id=None, reporter=None):
    reporter = reporter if reporter is not None else CoreReporter(config)

    def report(kind, status, message):
        reporter.emit(scene_id, transfer_id, kind, status, message)

    switch_scene_log(logger, f"{scene_id}")
    logger.info("SCENEID %s STARTING", scene_id)
    report("sync.started", "running", "Starting Whisparr sync")
    scene = None
    try:
        stash = StashHelpers.open_conn()
        scene_data = stash.find_scene(scene_id)
        if not scene_data:
            logger.error("Scene %s not found in Stash.", scene_id)
            report("sync.failed", "failed", "Scene not found in Stash")
            return "Failed"
        scene = StashSceneModel(
            **scene_data, stashdb_endpoint_substr=config.STASHDB_ENDPOINT_SUBSTR
        )
    except ValidationError as e:
        logger.exception("Scene data validation failed: %s", e)
        report("sync.failed", "failed", "Stash returned invalid scene data")
        return "Failed"
    except Exception as e:
        logger.exception("Unexpected error fetching scene: %s", e)
        report("sync.failed", "failed", "Could not read the scene from Stash")
        return "Failed"
    if scene.stashdb_id is None:
        logger.info("Waiting for a StashDB ID for %s", scene.title)
        report("sync.skipped", "ok", "Waiting for a StashDB ID")
        return "NoStashDB ID"
    logger.info("Processing scene: %s", scene.title)
    ignored_tag = has_ignored_tag(scene, config.IGNORE_TAGS)
    if ignored_tag:
        logger.info(
            "Scene '%s' skipped due to ignored tag: %s", scene.title, ignored_tag
        )
        report("sync.skipped", "ok", "Scene has an ignored tag")
        return "SkippedTag"
    whisparr = WhisparrInterface(config=config, stash_scene=scene, report=report)
    try:
        whisparr.process_scene()
        logger.info("Scene processing completed successfully.")
    except WhisparrError as e:
        logger.exception("Whisparr processing error: %s", e)
        if e.__cause__ is not None or str(e).startswith("HTTP "):
            report("sync.failed", "failed", "Whisparr request failed; see the Stash plugin log")
        else:
            report("sync.failed", "failed", str(e).replace(config.WHISPARR_KEY, "[redacted]"))
        return "Failed"
    except Exception as e:
        logger.exception(f"Unexpected error during scene processing: {e}")
        report("sync.failed", "failed", "Unexpected Whisparr Sync error; see the Stash plugin log")
        return "Failed"
    return "Success"


from typing import Optional


def bulk_processor(config: "PluginConfig") -> None:
    import sqlite3

    stash = StashHelpers.open_conn()
    generalconf: dict = stash.get_configuration().get("general")
    if not config.DEV_MODE:
        sqllite_db_loc: str = generalconf.get("databasePath")
    else:
        sqllite_db_loc = "stash-go.sqlite"
    # Fetch all scene IDs
    try:
        database_uri = Path(sqllite_db_loc).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM scenes ORDER BY id")
            scene_ids: list[int] = [row[0] for row in cursor.fetchall()]
        logger.info(f"Found {len(scene_ids)} scenes")
    except Exception as e:
        logger.error("Failed to initialize DB: %s", e)
        return

    if len(scene_ids) == 0:
        logger.error("Stash DB is empty! exiting")
        return

    config.STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    bulk_results = config.STATE_DIRECTORY / "bulk_results.csv"
    reporter = CoreReporter(config)
    outcomes = {}
    stash_log.progress(0)

    with open(bulk_results, "a", newline="") as records:
        writer = csv.writer(records)
        if bulk_results.stat().st_size == 0:
            writer.writerow(["scene_id", "success"])
            records.flush()
        for i, scene in enumerate(reversed(scene_ids), start=1):
            # stash_log.debug(f"Processing Scene: {scene}")
            try:
                success: str = process_single_scene(config, scene, reporter=reporter)
                writer.writerow([scene, success])
            except Exception as err:
                logger.error(f"main function error: {err}")
                success = "Failed"
                writer.writerow([scene, success])
            records.flush()
            outcomes[success] = outcomes.get(success, 0) + 1
            stash_log.progress(i / len(scene_ids))
            logger.info("Bulk progress: %d/%d scenes", i, len(scene_ids))
    logger.info("Bulk run finished: %s. Results: %s", outcomes, bulk_results)


def main(
    scene_id: Optional[int] = None,
    dev: Optional[bool] = False,
    bulk: Optional[bool] = False,
) -> None:
    global logger
    config: Optional["PluginConfig"] = preprocessor(dev)
    if not config:
        stash_log.error("Configuration could not be loaded; exiting.")
        return
    args = StashHelpers.STASH_DATA.get("args") or {}
    if args.get("mode") == "health":
        report_health(config)
        return
    if bulk:
        logger.debug("Bulk update started")
        bulk_processor(config=config)
    else:
        logger.debug("Starting stage 2 - get Scene ID")
        # 2. Determine scene ID
        hook_context: dict = args.get(
            "hookContext"
        ) or {}
        scene_id = scene_id or hook_context.get("id") or args.get("scene_id")
        if not scene_id:
            stash_log.info("No scene ID provided by hook.")
            if args.get("mode") == "bulk":
                logger.info("Bulk update started")
                bulk_processor(config=config)
            else:
                stash_log.error("No Scene ID and not bulk, exiting")
                return
        else:
            process_single_scene(config=config, scene_id=scene_id, transfer_id=args.get("transfer_id"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the main function with optional arguments."
    )
    parser.add_argument(
        "--scene_id", type=int, help="Optional scene ID to run", default=None
    )
    parser.add_argument("--dev", action="store_true", help="Run in development mode")
    parser.add_argument("--bulk", action="store_true", help="Batch Process all scenes")
    args = parser.parse_args()
    main(scene_id=args.scene_id, dev=args.dev, bulk=args.bulk)
