import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / 'plugins' / 'whisparr-sync'
sys.path.insert(0, str(PLUGIN))
spec = importlib.util.spec_from_file_location('sync', PLUGIN / 'whisparr-sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
sync.logger = logging.getLogger('test')


class Server:
    def __init__(self):
        self.calls = []
        self.files = []
        self.commands = {}
        self.status = 'completed'
        self.reject = False
        self.wrong_path = False
        self.rename_pending = False
        self.scene = sync.WhisparrScene(id=10, foreignId='scene-id', title='Scene',
            path='/library/scene', statistics={'movieFileCount': 0, 'sizeOnDisk': 0})

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        route = url.split('/api/v3/')[1]
        if method == 'POST':
            command = kwargs['body'].model_dump()
            cid = len(self.commands) + 1
            self.commands[cid] = command
            return 201, {'id': cid}
        if route == 'movie':
            return 200, [self.scene]
        if route == 'moviefile':
            return 200, list(self.files)
        if route == 'manualimport':
            # Whisparr ignores folder when movieId is supplied, scanning the
            # destination library rather than the external download directory.
            if 'movieId' in kwargs.get('params', {}):
                return 200, []
            return 200, [sync.ManualImportPreviewFile(
                path='/wrong/source.mkv' if self.wrong_path else '/downloads/source.mkv',
                folderName='downloads', size=42, quality=None,
                rejections=['blocked'] if self.reject else [])]
        if route.startswith('command/'):
            cid = int(route.split('/')[1])
            if self.status == 'completed' and self.commands[cid]['name'] == 'ManualImport':
                self.files = [{'id': 99, 'movieId': 10, 'path': '/library/scene/final.mkv'}]
            return 200, {'status': self.status}
        if route == 'rename':
            return 200, [{}] if self.rename_pending else []
        raise AssertionError(route)


def interface(tmp_path, server):
    config = sync.PluginConfig(WHISPARR_URL='http://whisparr', WHISPARR_KEY='test',
        MOVE_FILES=True, STATE_DIRECTORY=tmp_path, COMMAND_TIMEOUT=.001,
        COMMAND_POLL_INTERVAL=.001, PATH_MAPPING={'/downloads': '/stash/incoming'})
    scene = sync.StashSceneModel(files=[{'path': '/stash/incoming/source.mkv'}],
        stash_ids=[{'endpoint': 'https://stashdb.org/graphql', 'stash_id': 'scene-id'}])
    return sync.WhisparrInterface(config, scene, server)


def posted(server):
    return list(server.commands.values())


def test_import_waits_then_renames_with_file_ids(tmp_path):
    server = Server()
    interface(tmp_path, server).process_scene()
    assert posted(server)[0]['importMode'] == 'move'
    assert posted(server)[0]['files'][0]['path'] == '/downloads/source.mkv'
    preview_call = next(kwargs for method, url, kwargs in server.calls
                        if method == 'GET' and url.endswith('/manualimport'))
    assert preview_call['params'] == {'folder': '/downloads', 'filterExistingFiles': True}
    assert posted(server)[1] == {'name': 'RenameFiles', 'movieId': 10, 'files': [99]}
    routes = [u for _, u, _ in server.calls]
    assert routes.index('http://whisparr/api/v3/command/1') < routes.index('http://whisparr/api/v3/command/2')
    assert not list(tmp_path.glob('*.json'))


def test_repeat_hook_does_not_reimport(tmp_path):
    server = Server()
    interface(tmp_path, server).process_scene()
    interface(tmp_path, server).process_scene()
    assert sum(c['name'] == 'ManualImport' for c in posted(server)) == 1


@pytest.mark.parametrize('state', ['failed', 'aborted', 'queued', 'unexpected'])
def test_incomplete_import_never_renames(tmp_path, state):
    server = Server()
    server.status = state
    with pytest.raises(sync.WhisparrError):
        interface(tmp_path, server).process_scene()
    assert len(posted(server)) == 1
    assert list(tmp_path.glob('*.json'))


@pytest.mark.parametrize('attribute', ['reject', 'wrong_path'])
def test_rejected_or_wrong_full_path_never_imports(tmp_path, attribute):
    server = Server()
    setattr(server, attribute, True)
    with pytest.raises(sync.ManualImportError):
        interface(tmp_path, server).process_scene()
    assert not posted(server)


def test_unknown_post_outcome_blocks_retry(tmp_path):
    server = Server()
    api = interface(tmp_path, server)
    original = api.http_json
    def lost_response(method, url, **kwargs):
        if method == 'POST':
            raise sync.WhisparrError('response lost')
        return original(method, url, **kwargs)
    api.http_json = lost_response
    with pytest.raises(sync.WhisparrError):
        api.process_scene()
    with pytest.raises(sync.WhisparrError, match='outcome is unknown'):
        interface(tmp_path, server).process_scene()
    assert not posted(server)


def test_multiple_files_stop_without_mutation(tmp_path):
    server = Server()
    api = interface(tmp_path, server)
    api.stash_scene.files.append(sync.StashFile(path='/other.mkv'))
    with pytest.raises(sync.WhisparrError, match='Exactly one'):
        api.process_scene()
    assert not server.calls


def test_rename_failure_detected_even_when_command_completed(tmp_path):
    server = Server()
    server.rename_pending = True
    with pytest.raises(sync.WhisparrError, match='organization still differs'):
        interface(tmp_path, server).process_scene()


def test_wrong_file_ownership_stops(tmp_path):
    server = Server()
    server.files = [{'id': 99, 'movieId': 11, 'path': '/wrong.mkv'}]
    with pytest.raises(sync.WhisparrError, match='ownership'):
        interface(tmp_path, server).process_scene()
    assert not posted(server)


def test_recover_known_queued_command_without_reimport(tmp_path):
    server = Server()
    server.status = 'queued'
    with pytest.raises(sync.WhisparrError):
        interface(tmp_path, server).process_scene()
    server.status = 'completed'
    interface(tmp_path, server).process_scene()
    assert sum(c['name'] == 'ManualImport' for c in posted(server)) == 1


def test_longest_mapping_wins(tmp_path):
    api = interface(tmp_path, Server())
    api.config.PATH_MAPPING = {'/generic': '/stash', '/downloads': '/stash/incoming'}
    assert api._source_path() == Path('/downloads/source.mkv')


def test_overlapping_hook_stops_before_any_api_call(tmp_path):
    import fcntl
    import hashlib
    server = Server()
    key = hashlib.sha256(b'http://whisparr|scene-id').hexdigest()
    with (tmp_path / f'{key}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(sync.WhisparrError, match='already being processed'):
            interface(tmp_path, server).process_scene()
    assert not server.calls


def test_existing_whisparr_duplicates_never_replaced(tmp_path):
    server = Server()
    server.files = [{'id': i, 'movieId': 10, 'path': f'/library/{i}.mkv'} for i in (98, 99)]
    with pytest.raises(sync.WhisparrError, match='Multiple Whisparr files'):
        interface(tmp_path, server).process_scene()
    assert not posted(server)
