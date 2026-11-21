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
        self.rename_needed = True
        self.unknown_movie = False
        self.reprocess_wrong_identity = False
        self.exists = True
        self.scene = sync.WhisparrScene(id=10, foreignId='scene-id', title='Scene',
            path='/library/scene', statistics={'movieFileCount': 0, 'sizeOnDisk': 0})

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        route = url.split('/api/v3/')[1]
        if method == 'POST' and route == 'movie':
            self.exists = True
            return 201, self.scene
        if method == 'POST' and route == 'manualimport':
            item = dict(kwargs['body'][0])
            item['movie'] = {'id': 10, 'foreignId': 'wrong' if self.reprocess_wrong_identity else 'scene-id'}
            item['rejections'] = [{'reason': 'blocked'}] if self.reject else []
            return 200, [item]
        if method == 'POST':
            command = kwargs['body'].model_dump()
            cid = len(self.commands) + 1
            self.commands[cid] = command
            return 201, {'id': cid}
        if route == 'movie':
            return 200, [self.scene] if self.exists else []
        if route == 'qualityprofile':
            return 200, [{'id': 1, 'name': 'Any'}]
        if route == 'rootfolder':
            return 200, [{'path': '/library'}]
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
                rejections=['blocked'] if self.reject else ([{'reason': 'Unknown Movie'}] if self.unknown_movie else []))]
        if route.startswith('command/'):
            cid = int(route.split('/')[1])
            if self.status == 'completed' and self.commands[cid]['name'] == 'ManualImport':
                self.files = [{'id': 99, 'movieId': 10, 'path': '/library/scene/final.mkv'}]
            if self.status == 'completed' and self.commands[cid]['name'] == 'RenameFiles':
                self.rename_needed = False
            return 200, {'status': self.status}
        if route == 'rename':
            return 200, [{}] if self.rename_pending or self.rename_needed else []
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
    assert sum(c['name'] == 'RenameFiles' for c in posted(server)) == 1


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
        if method == 'POST' and url.endswith('/command'):
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


@pytest.mark.parametrize('folder', ['missing', None, ''])
def test_base_folder_import_does_not_require_folder_metadata(tmp_path, folder):
    server = Server()
    original = server.__call__
    def api(method, url, **kwargs):
        if method == 'GET' and url.endswith('/manualimport'):
            raw = {'path': '/downloads/source.mkv', 'size': 42, 'quality': None}
            if folder != 'missing':
                raw['folderName'] = folder
            return 200, [sync.ManualImportPreviewFile(**raw)]
        return original(method, url, **kwargs)
    workflow = interface(tmp_path, server)
    workflow.http_json = api
    workflow.process_scene()
    payload = sync.ManualImportCommand(**posted(server)[0]).model_dump(exclude_none=True)
    assert payload['files'][0]['path'] == '/downloads/source.mkv'
    assert 'folderName' not in payload['files'][0]


def test_missing_folder_name_preserves_rejections(tmp_path):
    server = Server()
    server.reject = True
    original = server.__call__
    def api(method, url, **kwargs):
        if method == 'GET' and url.endswith('/manualimport'):
            return 200, [sync.ManualImportPreviewFile(
                path='/downloads/source.mkv', size=42, quality=None,
                rejections=[{'reason': 'Unknown Movie'}])]
        return original(method, url, **kwargs)
    workflow = interface(tmp_path, server)
    workflow.http_json = api
    with pytest.raises(sync.ManualImportError, match='blocked'):
        workflow.process_scene()
    assert not posted(server)


def test_new_scene_added_then_assigned_by_stashdb_before_import(tmp_path):
    server = Server()
    server.exists = False
    server.unknown_movie = True
    interface(tmp_path, server).process_scene()
    add = next(kwargs['body'] for method, url, kwargs in server.calls
               if method == 'POST' and url.endswith('/movie'))
    assert add.foreignId == 'scene-id'
    assigned = next(kwargs['body'][0] for method, url, kwargs in server.calls
                    if method == 'POST' and url.endswith('/manualimport'))
    assert assigned['movieId'] == 10
    assert assigned['path'] == '/downloads/source.mkv'
    assert posted(server)[0]['name'] == 'ManualImport'
    assert posted(server)[1]['name'] == 'RenameFiles'


def test_reassignment_to_wrong_identity_never_imports(tmp_path):
    server = Server()
    server.reprocess_wrong_identity = True
    with pytest.raises(sync.ManualImportError, match='intended StashDB identity'):
        interface(tmp_path, server).process_scene()
    assert not posted(server)


def test_already_organized_scene_submits_no_commands(tmp_path):
    server = Server()
    server.files = [{'id': 99, 'movieId': 10, 'path': '/library/scene/final.mkv'}]
    server.rename_needed = False
    interface(tmp_path, server).process_scene()
    assert not posted(server)


def test_bulk_reads_database_readonly_and_records_each_outcome(tmp_path, monkeypatch):
    import csv
    import sqlite3
    from types import SimpleNamespace
    database = tmp_path / 'stash.sqlite'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE scenes (id INTEGER PRIMARY KEY)')
        conn.executemany('INSERT INTO scenes VALUES (?)', [(1,), (2,), (3,)])
    real_connect = sqlite3.connect
    connections = []
    def connect(database_arg, **kwargs):
        connections.append((database_arg, kwargs))
        return real_connect(database_arg, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    stash = SimpleNamespace(get_configuration=lambda: {'general': {'databasePath': str(database)}})
    monkeypatch.setattr(sync.StashHelpers, 'open_conn', lambda: stash)
    progress = []
    monkeypatch.setattr(sync.stash_log, 'progress', progress.append)
    def process(config, scene_id, reporter=None):
        if scene_id == 2:
            raise RuntimeError('scene failed')
        return 'Success' if scene_id == 3 else 'NoStashDB ID'
    monkeypatch.setattr(sync, 'process_single_scene', process)
    config = interface(tmp_path / 'state', Server()).config
    sync.bulk_processor(config)
    assert connections == [(database.as_uri() + '?mode=ro', {'uri': True})]
    with (config.STATE_DIRECTORY / 'bulk_results.csv').open() as record:
        assert list(csv.reader(record)) == [
            ['scene_id', 'success'], ['3', 'Success'], ['2', 'Failed'], ['1', 'NoStashDB ID']]
    assert progress[0] == 0 and progress[-1] == 1


def test_core_reports_commands_and_registered_path(tmp_path, monkeypatch):
    server = Server()
    api = interface(tmp_path, server)
    config = api.config.model_copy(update={
        'SMUTWORKS_CORE_URL': 'http://core:8794',
        'SMUTWORKS_CORE_TOKEN': 'reporter-secret',
        'SMUTWORKS_STASH_INSTANCE': 'stash-solo',
    })
    monkeypatch.setattr(sync.StashHelpers, 'open_conn', lambda: FakeStash(api.stash_scene))
    monkeypatch.setattr(sync, 'WhisparrInterface',
        lambda **kwargs: type(api)(http_func=server, **kwargs))
    monkeypatch.delenv('SMUTWORKS_CORE_TOKEN_FILE', raising=False)
    monkeypatch.delenv('SMUTWORKS_CORE_TOKEN', raising=False)
    received = []

    def accept(_, url, *, data, headers, allow_redirects, **kwargs):
        assert url == 'http://core:8794/v1/events'
        assert headers['Authorization'] == 'Bearer reporter-secret'
        assert allow_redirects is False
        received.append(json.loads(data))
        response = sync.requests.Response()
        response.status_code = 200
        response._content_consumed = True
        return response

    monkeypatch.setattr(sync.requests.Session, 'post', accept)
    assert sync.process_single_scene(config, 42, transfer_id='seedbox-pull:test') == 'Success'
    assert [event['kind'] for event in received] == [
        'sync.started', 'import.accepted', 'import.completed',
        'rename.accepted', 'rename.completed', 'file.registered',
    ]
    assert all(event['source'] == 'whisparr-sync' and event['stash_instance'] == 'stash-solo'
               and event['scene_id'] == '42' and event['transfer_id'] == 'seedbox-pull:test'
               for event in received)
    assert 'command 1' in received[1]['message']
    assert '/library/scene/final.mkv' in received[-1]['message']
    assert len({event['event_id'] for event in received}) == len(received)
    assert not list((tmp_path / 'core-outbox').glob('*.json'))


class FakeStash:
    def __init__(self, scene):
        self.scene = scene

    def find_scene(self, _):
        return {
            'files': [{'path': str(path)} for path in self.scene.paths],
            'stash_ids': [{'endpoint': 'https://stashdb.org/graphql', 'stash_id': 'scene-id'}],
        }


def test_scene_without_stashdb_id_reports_an_informational_skip(tmp_path, monkeypatch):
    server = Server()
    api = interface(tmp_path, server)
    stash = FakeStash(api.stash_scene)
    monkeypatch.setattr(stash, 'find_scene', lambda _: {'files': [], 'stash_ids': []})
    monkeypatch.setattr(sync.StashHelpers, 'open_conn', lambda: stash)
    received = []

    class Reporter:
        def emit(self, *args):
            received.append(args)

    assert sync.process_single_scene(api.config, 42, reporter=Reporter()) == 'NoStashDB ID'
    assert received[-1] == (42, None, 'sync.skipped', 'ok', 'Waiting for a StashDB ID')
    assert not server.calls


def test_core_outage_keeps_reports_and_does_not_interrupt_import(tmp_path, monkeypatch):
    server = Server()
    api = interface(tmp_path, server)
    config = api.config.model_copy(update={
        'SMUTWORKS_CORE_URL': 'http://core:8794',
        'SMUTWORKS_CORE_TOKEN': 'reporter-secret',
    })
    monkeypatch.delenv('SMUTWORKS_CORE_TOKEN_FILE', raising=False)
    monkeypatch.delenv('SMUTWORKS_CORE_TOKEN', raising=False)
    monkeypatch.setattr(sync.StashHelpers, 'open_conn', lambda: FakeStash(api.stash_scene))
    monkeypatch.setattr(sync, 'WhisparrInterface',
        lambda **kwargs: type(api)(http_func=server, **kwargs))
    attempted = []

    def offline(_, url, *, data, **kwargs):
        attempted.append(json.loads(data)['event_id'])
        raise sync.requests.ConnectionError('Core unavailable')

    monkeypatch.setattr(sync.requests.Session, 'post', offline)
    assert sync.process_single_scene(config, 42) == 'Success'
    assert len(attempted) == 1
    paths = sorted((tmp_path / 'core-outbox').glob('*.json'))
    queued = [json.loads(path.read_bytes()) for path in paths]
    assert len(queued) == 6
    assert queued[0]['event_id'] == attempted[0]
    assert server.files[0]['path'] == '/library/scene/final.mkv'
    delivered = []

    def online(_, url, *, data, **kwargs):
        delivered.append(json.loads(data))
        response = sync.requests.Response()
        response.status_code = 200
        response._content_consumed = True
        return response

    monkeypatch.setattr(sync.requests.Session, 'post', online)
    sync.CoreReporter(config).flush()
    assert delivered == queued
    assert not list((tmp_path / 'core-outbox').glob('*.json'))


def test_health_reports_to_kuma_even_when_core_is_unavailable(tmp_path, monkeypatch):
    config = interface(tmp_path, Server()).config.model_copy(update={
        'SMUTWORKS_CORE_URL': 'http://core:8794',
        'SMUTWORKS_CORE_TOKEN': 'whisparr-reporter',
    })
    monkeypatch.delenv('SMUTWORKS_CORE_TOKEN', raising=False)
    monkeypatch.delenv('SMUTWORKS_CORE_TOKEN_FILE', raising=False)
    monkeypatch.setenv('SMUTWORKS_KUMA_PUSH_URL', 'http://kuma/api/push/test?status=up')
    calls = []

    def get(_, url, **kwargs):
        calls.append(('GET', url, kwargs))
        response = sync.requests.Response()
        response.status_code = 503 if url.startswith('http://whisparr/') else 200
        response._content = b'{"ok":true}'
        response._content_consumed = True
        return response

    def post(_, url, **kwargs):
        calls.append(('POST', url, kwargs))
        raise sync.requests.ConnectionError('Core unavailable')

    monkeypatch.setattr(sync.requests.Session, 'get', get)
    monkeypatch.setattr(sync.requests.Session, 'post', post)
    sync.report_health(config)
    assert [(method, url) for method, url, _ in calls] == [
        ('GET', 'http://whisparr/api/v3/system/status'),
        ('POST', 'http://core:8794/v1/health'),
        ('GET', 'http://kuma/api/push/test'),
    ]
    report = calls[1][2]['json']
    assert report['source'] == 'whisparr-sync'
    assert report['status'] == calls[2][2]['params']['status'] == 'down'
    assert report['latency_ms'] == calls[2][2]['params']['ping']
    assert calls[1][2]['headers'] == {'Authorization': 'Bearer whisparr-reporter'}
    assert 'headers' not in calls[2][2]
