# Whisparr import and organization modernization

## Scope

Stash identifies the scene and supplies its existing source path. This plugin adds
or locates the Whisparr scene and submits that file for import. Whisparr alone moves
and renames it. The user's existing Whisparr notifications and separate Stash
plugin remain responsible for Stash updates. This fork does not rescan Stash,
change Stash metadata, deduplicate media, or write the Stash database.

No production NAS was accessed or changed during development.

## Findings

Reviewed upstream plugin commit `81416dffc89cf960738fd7ff8db13401c865a02b`.
The installed-code excerpt in the referenced conversation has the same incorrect
RenameCommand declaration and invocation. The full installed file was not
available, so other local modifications remain unverified.

* `RenameFiles` requires a scalar `movieId` and a `files` array containing movie-file
  IDs. `movieIds` belongs to other commands; it is not a replacement for these fields.
* Upstream moves files locally with `Path.replace` before import, risking overwrites
  and cross-filesystem failures. This implementation removes that mover entirely.
* HTTP 201 means a command was queued, not that import finished. Upstream immediately
  queues rename and logs import success before completion.
* Upstream matches by basename, processes only the last matching file in a scene,
  and treats matching file counts as evidence of import. These are insufficient.
* Upstream does not implement the return path to Stash. The user's already-configured
  Whisparr notifications provide that integration; duplicating it is out of scope.

Whisparr source reviewed at eros commit
`cc3fb2abcf60f7c0048eb0294015d291b82bde08`:

* https://github.com/Whisparr/Whisparr/blob/cc3fb2abcf60f7c0048eb0294015d291b82bde08/src/NzbDrone.Core/MediaFiles/Commands/RenameFilesCommand.cs
* https://github.com/Whisparr/Whisparr/blob/cc3fb2abcf60f7c0048eb0294015d291b82bde08/src/NzbDrone.Core/MediaFiles/RenameMovieFileService.cs
* https://github.com/Whisparr/Whisparr/blob/cc3fb2abcf60f7c0048eb0294015d291b82bde08/src/NzbDrone.Core/MediaFiles/MovieImport/Manual/ManualImportService.cs
* https://github.com/Whisparr/Whisparr/blob/cc3fb2abcf60f7c0048eb0294015d291b82bde08/src/Whisparr.Api.V3/Movies/RenameMovieController.cs

The exact `3.6.6.1943` build was not available locally. An upstream tag lookup for
`v3.6.6` returned no matches. API compatibility is source-supported, but has NOT
been proven against that installed binary. Do not interpret offline tests as
live certification.

## New workflow

1. Lock by server URL and StashDB ID to serialize plugin hooks in one container.
2. Resume a previously submitted command by ID. If submission's outcome is unknown,
   stop for inspection instead of blindly repeating it.
3. Locate the exact StashDB identity or create it with the configured root/profile.
4. Query Whisparr movie-file records. If one exists, skip import/replacement. If more
   than one exists, stop for review. This intentionally does not perform upgrades.
5. Translate the Stash source path into Whisparr's namespace using the longest
   matching PATH_MAPPING entry. Preview that source directory and match the complete
   source path. Stop on rejected, missing, or ambiguous previews.
6. Submit `ManualImport` with `importMode: move`. Wait for completion and require
   exactly one registered file before renaming.
7. Submit `{"name":"RenameFiles","movieId":10,"files":[99]}` using the actual IDs.
8. Wait, require an empty rename preview, and log the final API-reported file path.
   The rename service can catch per-file failures internally, so command completion
   alone is insufficient. An empty preview checks naming/folder expectations;
   this does not independently prove that disk bytes exist or notifications arrived.

## Configuration and rollout

Python 3.11+ on Linux (NAS/container) or macOS is required; locks use `fcntl`.
Keep Whisparr's naming rules and existing Stash notification integration enabled.
`MOVE_FILES = true` authorizes Whisparr to consume an external source. False permits
only sources already beneath the existing Whisparr scene folder; it never silently
falls back to copying an external source.

PATH_MAPPING remains **Whisparr prefix -> Stash container prefix**. Example:

```toml
MOVE_FILES = true
WHISPARR_RENAME = true

[PATH_MAPPING]
"/downloads" = "/stash/incoming"
"/media" = "/stash/library"
```

A source `/stash/incoming/scene.mkv` is submitted as `/downloads/scene.mkv`.
Whisparr must have read/write access to the source and destination. Whisparr handles
cross-filesystem moves; the plugin never uses local rename/replace.

Optional top-level settings (place BEFORE `[PATH_MAPPING]`):

```toml
COMMAND_TIMEOUT = 300
COMMAND_POLL_INTERVAL = 1
# Use an absolute persistent directory writable by the plugin:
# STATE_DIRECTORY = "/root/.stash/plugins/whisparr-sync/state"
```

The default state directory is `state/` beside the plugin. Preserve this directory
across plugin/container updates. Locks coordinate only processes sharing this state
directory. Do not run multiple independent installations against the same scenes.
A different URL spelling also creates a different lock namespace.

Checkpoint files contain only the command name/ID. If a command fails or a response
is lost, inspect Whisparr's queue, history, file records, and disk before manually
clearing that scene's checkpoint. Never delete all checkpoints to force a bulk retry.
A timed-out command with a known ID is polled on the next run, not submitted again.

Before deployment, back up the current plugin/config and verify actual container
mount paths. Disable the update hook and bulk task during a controlled trial.
Use one disposable representative scene, then a few scenes across source mounts:
verify import completion, final folder/name, source removal, and receipt of the
existing Stash notification. Also trigger a repeat hook and confirm no second import.
Re-enable bulk processing only after these checks pass. Deployment and NAS access
require separate explicit authorization.

Initial implementation deliberately stops on multi-file Stash scenes instead of
silently choosing a version. A single existing Whisparr file is retained regardless
of whether a newly supplied source is an upgrade. Configure an explicit replacement
policy separately if that is needed.

## Validation

Run `python -m pytest -q tests` with the plugin requirements installed.
Tests use a simulated Whisparr server; no production endpoints or media are touched.
