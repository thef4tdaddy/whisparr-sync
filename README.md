# Whisparr Sync

## Report to SmutWorks Core

Enable the `whisparr-sync` reporter on [SmutWorks Core](https://github.com/thef4tdaddy/smutworks-suite-core)
by adding a dedicated token to Core's `CORE_REPORT_TOKENS`:

```dotenv
CORE_REPORT_TOKENS='{"whisparr-sync":"your-reporter-token"}'
```

In Stash's Whisparr Sync settings, set **SmutWorks Core URL** to the Core base URL
reachable from the Stash container and **SmutWorks Stash instance** to `solo` (or
another stable label if configuring multiple instances). Set `SMUTWORKS_CORE_TOKEN`
in the plugin's local `config.toml` or the Stash container's environment. To use a
mounted secret, set `SMUTWORKS_CORE_TOKEN_FILE` in the container instead.

The plugin sends sync starts, accepted and completed import/rename commands,
final paths registered by Whisparr, and skipped or failed syncs to `/v1/events`.
Reports include the Stash instance and scene ID. An explicit plugin invocation
can pass `scene_id` and `transfer_id` to attach those reports to a Core transfer.
Reports that cannot reach Core are saved under the plugin's `state/core-outbox`
directory and retried on a later sync. Preserve the state directory across updates.

### Optional uptime heartbeat

In Stash, run or schedule **Report SmutWorks Health**. It checks that Stash can
reach the authenticated Whisparr API and sends the result to Core's `/v1/health`.
Add the plugin's existing Core token to Core's `CORE_HEALTH_TOKENS` map under
`whisparr-sync`. To also report to Uptime Kuma, set `SMUTWORKS_KUMA_PUSH_URL` in
the Stash container to the full URL from a dedicated Kuma Push monitor.
Either destination can be configured independently. Set **SmutWorks heartbeat
interval** to match the scheduler and Kuma monitor. Regular import hooks keep
reporting job events. Running the health task never starts an import.

Identify a file in Stash with a StashDB ID, then let Whisparr add the scene,
import the file, and move/rename it using Whisparr's library settings. The scene
need not already exist in Whisparr, and following its studio is not required.

## Install

1. In **Stash → Settings → Plugins → Available Plugins**, add:

   ```text
   https://github.com/thef4tdaddy/stash-curator/releases/download/stashui-preview/index.yml
   ```

   This shared custom source offers Curator and Whisparr Sync separately.
2. Install **Whisparr Sync** and reload plugins. When replacing a manual copy,
   back it up and move it outside Stash's plugins directory first. Preserve its
   `config.toml` and `state/`. Do not run two copies of the hook.
3. Provide **Python 3.11+ on Linux/macOS** and install the packaged requirements
   in the Python environment used by Stash (inside its container when using Docker):

   ```bash
   python -m pip install -r /path/to/installed/whisparr-sync/requirements.txt
   ```

   Stash's plugin manager does not install Python dependencies for you.
4. Set the Whisparr URL (including `http://` or `https://`), API key, quality
   profile, and destination root. Enable **Move Files** and **Enable Whisparr Rename**.
   Configure output naming rules in Whisparr.
5. **Set up Whisparr's existing Stash notification connection for import and rename
   updates.** Verify it targets the intended Stash instance and follow its setup
   instructions for any companion integration. That connection handles the return
   update. This plugin adds no callback, starts no Stash scan, and does not change
   Stash paths or metadata.
6. Check container mounts and permissions. Whisparr must read/move the source and
   write the destination. Stash must see the final library location for its existing
   notification integration to recognize the moved file.
7. Save one identified Stash scene and verify import, naming, and the return update
   before bulk use. This flow has been verified live on Whisparr 3.6.6.1943.

## Configuration and paths

Files can be directly in the import base folder or nested below it. No per-scene
input folder is required. Whisparr alone determines the destination layout.

Use Stash's plugin settings or create `config.toml` beside the plugin. The package
includes `config.example.toml` instead of overwriting existing configuration.
Stash UI settings override file settings. The key option is `WHISPARR_KEY`.

When both containers mount the same host storage at `/media`, no mapping is needed.
Otherwise `PATH_MAPPING` maps **Whisparr prefixes → Stash container prefixes**:

```toml
WHISPARR_URL = "http://whisparr:6969"
WHISPARR_KEY = "your-api-key"
MOVE_FILES = true
WHISPARR_RENAME = true
QUALITY_PROFILE = "Any"
ROOT_FOLDER = "/media/library"
COMMAND_TIMEOUT = 300
COMMAND_POLL_INTERVAL = 1

[PATH_MAPPING]
"/downloads" = "/stash/incoming"
"/media" = "/stash/library"
```

A Stash file `/stash/incoming/example.mp4` becomes `/downloads/example.mp4` for
Whisparr. Longest-prefix matching applies. Use container paths, not NAS host paths.
Top-level TOML settings belong before `[PATH_MAPPING]`.

Other options include `MONITORED` (default true), `IGNORE_TAGS`, and
`STASHDB_ENDPOINT_SUBSTR` (default `stashdb.org`). An empty root selects Whisparr's
first root; select one explicitly when you have multiple libraries.

## Workflow

1. Read the Stash scene's saved StashDB identity; skip unidentified/ignored scenes.
2. Find that identity in Whisparr or add it using the configured root/profile.
3. Retain any existing Whisparr file instead of importing/replacing it again.
4. Match the complete source path, explicitly assign the known scene in Whisparr's
   import preview, and check the resulting rejections. This resolves filename-based
   "Unknown Movie" results without bypassing remaining rejection checks.
5. Submit `ManualImport` in move mode and wait for completion.
6. Check the rename preview; rename only when needed, verify the result, and log
   Whisparr's final file path.
7. Whisparr's existing notifications update Stash. Subsequent hooks reuse the file.

The plugin never moves files itself or copies them into staging folders. It
requires one source file per Stash scene and stops on multiple existing Whisparr
files rather than choosing or replacing versions automatically.

## Bulk processing

Run **Sync All Scenes to Whisparr** from Stash's plugin tasks after individual tests
work. Run one bulk task at a time. It processes scenes sequentially, waiting for
Whisparr commands before moving on; Stash notification/fingerprint jobs can continue
past the end of the bulk task.

Bulk mode reads scene IDs through a read-only SQLite connection, shows task progress,
and writes each outcome immediately to `state/bulk_results.csv`. It continues past
failed, unidentified, and ignored scenes and logs a final summary. Existing files
are not re-imported; already-correct names avoid another rename command. Bulk does
not upgrade or replace existing Whisparr files.

Per-scene locks prevent simultaneous hooks in one installation from importing the
same scene. Preserve `state/` across updates. Timed-out commands with known IDs are
checked on the next run. For an unknown submission outcome or failed command,
inspect Whisparr before clearing that scene's checkpoint. Do not clear all
checkpoints to force a retry.

## Troubleshooting

* Nothing runs: reload plugins, save a scene with a StashDB ID, and check plugin logs.
* No exact-path match: compare both containers' mounts and returned preview paths.
  The plugin never falls back to basename matching.
* Rejected source: fix Whisparr's rejection after explicit identity assignment.
* Stash has an old path: check Whisparr's existing notification connection, its
  target instance, permissions, and visibility of the final destination.
* Installing/updating does not automatically process your library. Save an
  individual scene or intentionally start bulk processing.

See [modernization notes](docs/modernization.md) for API references and recovery.

## License

Upstream does not currently include an explicit license. Contact its author for
clarification on usage rights.
