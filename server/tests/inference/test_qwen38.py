"""Pinned Qwen3.8 delivery against synthetic metadata and small local blobs."""
from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/inference'))
import pinned_model_install as installer


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def setup_tree(root):
    for key in installer.MODEL_KEYS:
        for name in (f'config/inference.{key}.example.json', f'config/models/{key}.source.json'):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
    primary = json.loads((ROOT / 'config/inference.coder.example.json').read_text())
    primary.update(ExecutablePath=sys.executable, Model='original', ProfileId='original',
                   ModelLockPath='config/models/original.local.json', KvCacheType='q4_0')
    dump(root / 'config/original.json', primary)
    dump(root / primary['ModelLockPath'], {
        'engine_version': primary['EngineVersion'], 'model': {'name': 'original', 'digest': 'a' * 64},
        'template_sha256': hashlib.sha256(b'original-template').hexdigest(),
        'capabilities': primary['RequiredCapabilities']})
    server = {'provider': 'Ollama', 'manage_engine': True, 'inference_config': 'config/original.json',
              'additional_inference_configs': [], 'data_directory': '.local/private',
              'port': 19001, 'private_sentinel': 'preserve-private-field'}
    path = root / 'config/server.local.json'
    dump(path, server)
    return path, primary


def engine_for(root, failure=None, seen=None):
    @contextmanager
    def engine(project, cfg, work):
        if seen is not None:
            seen.append(dict(cfg))
        if failure == 'concurrent':
            path = root / 'config/server.local.json'
            current = json.loads(path.read_bytes())
            dump(path, {**current, 'port': 19002})
        current = [json.loads((root / 'config/original.json').read_bytes())]
        for key in installer.MODEL_KEYS:
            path = root / f'config/inference.{key}.local.json'
            if path.exists():
                current.append(json.loads(path.read_bytes()))
        current.append(cfg)
        profiles = {item['Model']: item for item in current}
        def metadata(name):
            item = profiles[name]
            source = next(json.loads((root / f'config/models/{key}.source.json').read_bytes())
                          for key in installer.MODEL_KEYS
                          if json.loads((root / f'config/models/{key}.source.json').read_bytes())['model'] == name)
            return item, source
        def fetch(endpoint, body=None):
            if endpoint == '/api/version':
                return {'version': 'wrong' if failure == 'version' else cfg['EngineVersion']}
            if endpoint == '/api/tags':
                rows = [{'name': 'original', 'digest': 'a' * 64}]
                for name in profiles:
                    if name != 'original':
                        item, source = metadata(name)
                        rows.append({'name': name, 'digest': 'b' * 64 if failure == 'digest' else source['manifest_sha256'],
                                     'details': {'quantization_level': item['ExpectedQuantization']}})
                return {'models': rows}
            if endpoint == '/api/show':
                if body['model'] == 'original':
                    return {'template': 'original-template', 'details': {'quantization_level': 'Q5_K_M'},
                            'capabilities': primary_caps}
                item, source = metadata(body['model'])
                return {'template': 'target-template', 'details': {'quantization_level': item['ExpectedQuantization']},
                        'model_info': {'general.architecture': 'wrong' if failure == 'architecture' else source['architecture']},
                        'capabilities': [] if failure == 'capabilities' else source['required_capabilities'],
                        'thinking': {'values': [] if failure == 'thinking' else item.get('ThinkingLevels', [False, True])}}
            if endpoint == '/api/generate':
                assert body['options']['num_gpu'] == 999 and body['prompt'] == ''
                assert body['options']['num_ctx'] == cfg['NumCtx']
                assert body['options']['num_batch'] == cfg['NumBatch']
                return {'done': failure != 'load'}
            if endpoint == '/api/ps':
                return {'models': [{'name': cfg['Model'], 'size': 100, 'size_vram': 90 if failure == 'gpu' else 100,
                                    'context_length': cfg['NumCtx'] - 1 if failure == 'window' else cfg['NumCtx']}]}
            raise AssertionError(endpoint)
        primary_caps = profiles['original']['RequiredCapabilities']
        yield fetch
    return engine


@pytest.mark.parametrize('key', installer.MODEL_KEYS)
@pytest.mark.parametrize('failure', [None, 'gpu', 'window', 'architecture', 'digest', 'capabilities', 'version', 'load', 'concurrent', 'save'])
def test_install_preserves_private_primary_and_rolls_back(root_dir, monkeypatch, key, failure):
    root = root_dir
    server, primary = setup_tree(root)
    original = server.read_bytes()
    monkeypatch.setattr(installer.legacy, 'offline', lambda *args: nullcontext())
    download = Mock()
    monkeypatch.setattr(installer, 'download_model', download)
    if failure == 'save':
        monkeypatch.setattr(installer.legacy.support, 'atomic_write', Mock(side_effect=OSError()))
    config = root / f'config/inference.{key}.local.json'
    identity = root / f'config/models/{key}.local.json'
    seen = []
    if failure:
        with pytest.raises((ValueError, OSError)):
            installer.install(key, root, server_config=server, engine_factory=engine_for(root, failure, seen))
        assert not config.exists() and not identity.exists()
        assert server.read_bytes() == original if failure != 'concurrent' else json.loads(server.read_bytes())['port'] == 19002
    else:
        result = installer.install(key, root, server_config=server, engine_factory=engine_for(root, seen=seen))
        current = json.loads(server.read_bytes())
        assert current['inference_config'] == 'config/original.json'
        assert current['additional_inference_configs'] == [f'config/inference.{key}.local.json']
        assert current['private_sentinel'] == 'preserve-private-field' and current['port'] == 19001
        assert Path(result['backup']).read_bytes() == original
        cfg = json.loads(config.read_bytes())
        assert cfg['KvCacheType'] == 'q8_0'
        assert cfg['RequiredCapabilities'] == ['completion', 'tools', 'thinking']
        assert seen[0]['KvCacheType'] == cfg['KvCacheType'] != primary['KvCacheType']
        cfg.update(NumBatch=32, NumCtx=8192, NumPredict=4096)
        dump(config, cfg)
        before = {p: p.read_bytes() for p in (config, identity, server)}
        assert installer.install(key, root, server_config=server, engine_factory=engine_for(root))['backup'] is None
        assert all(path.read_bytes() == raw for path, raw in before.items())
        with pytest.raises(ValueError, match='调优不同'):
            installer.install(key, root, server_config=server, context=16384)
    assert download.call_count >= 1


@pytest.fixture
def root_dir(tmp_path):
    root = tmp_path / '中文 空格工程'
    root.mkdir()
    return root


def test_qwen38_is_additive_and_prepare_only_does_not_commit(root_dir, monkeypatch):
    server, _ = setup_tree(root_dir)
    original = server.read_bytes()
    monkeypatch.setattr(installer.legacy, 'offline', lambda *args: nullcontext())
    monkeypatch.setattr(installer, 'download_model', Mock())
    installer.install('qwen38', root_dir, server_config=server, prepare_only=True, engine_factory=engine_for(root_dir))
    assert server.read_bytes() == original
    for key in ('qwen38',):
        installer.install(key, root_dir, server_config=server, engine_factory=engine_for(root_dir))
    current = json.loads(server.read_bytes())
    assert current['inference_config'] == 'config/original.json'
    assert current['additional_inference_configs'] == ['config/inference.qwen38.local.json']
    assert len(list(server.parent.glob('*.bak'))) == 1


def test_stopped_service_and_existing_lock_are_required(root_dir, monkeypatch):
    server, _ = setup_tree(root_dir)
    download = Mock()
    monkeypatch.setattr(installer, 'download_model', download)
    from aiserver.operations.lock import InstanceLock
    lock = InstanceLock(root_dir / '.local/private').acquire()
    try:
        with pytest.raises(ValueError, match='仍在运行'):
            installer.install('qwen38', root_dir, server_config=server)
        download.assert_not_called()
    finally:
        lock.release()
    monkeypatch.setattr(installer.legacy, 'offline', lambda *args: nullcontext())
    dump(root_dir / 'config/models/qwen38.local.json', {'preserve': 'different-lock'})
    with pytest.raises(ValueError, match='身份锁不同'):
        installer.install('qwen38', root_dir, server_config=server, engine_factory=engine_for(root_dir))
    assert json.loads((root_dir / 'config/models/qwen38.local.json').read_bytes()) == {'preserve': 'different-lock'}
    assert not (root_dir / 'config/inference.qwen38.local.json').exists()


def tiny_source():
    blobs = [b'config-bytes', b'model-bytes']
    items = [{'digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'size': len(data),
              'mediaType': 'application/vnd.ollama.image.model' if index else 'config'}
             for index, data in enumerate(blobs)]
    raw = json.dumps({'schemaVersion': 2, 'config': items[0], 'layers': [items[1]]})
    return {'manifest': raw, 'manifest_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'manifest_path': 'manifests/fixture.test/library/model/q4', 'registry': 'https://fixture.test'}, items, blobs


def test_download_verifies_reuse_corruption_and_never_publishes_partial_manifest(tmp_path, monkeypatch):
    source, items, blobs = tiny_source()
    origin = tmp_path / 'origin/blobs'
    origin.mkdir(parents=True)
    for item, data in zip(items, blobs):
        (origin / item['digest'].replace(':', '-')).write_bytes(data)
    cfg = {'ModelsPath': 'store'}
    path = installer.download_model(tmp_path, cfg, source, origin.parent, allow_download=False)
    assert path.read_bytes() == source['manifest'].encode()
    (tmp_path / 'store/blobs' / items[1]['digest'].replace(':', '-')).write_bytes(b'bad')
    with pytest.raises(ValueError, match='大小不符|SHA256'):
        installer.download_model(tmp_path, cfg, source)
    source['manifest_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='清单SHA256'):
        installer.download_model(tmp_path, cfg, source)
    source, items, blobs = tiny_source()
    monkeypatch.setattr(installer, 'download', Mock(side_effect=OSError('network-failure')))
    with pytest.raises(OSError):
        installer.download_model(tmp_path, {'ModelsPath': 'new-store'}, source)
    assert not (tmp_path / 'new-store' / source['manifest_path']).exists()


def test_cli_reports_failure_for_qwen38(monkeypatch, capsys):
    monkeypatch.setattr(installer.dependencies, 'ensure_dependencies', Mock())
    prepare = Mock(side_effect=[ValueError('Qwen failed preflight'), {}])
    monkeypatch.setattr(installer, 'install', prepare)
    monkeypatch.setattr(sys, 'argv', ['Install-Qwen38.py'])
    assert installer.main() == 1
    assert [call.args[0] for call in prepare.call_args_list] == ['qwen38']
    assert 'qwen38' in capsys.readouterr().out


def test_delivered_zip_contains_entrypoints_and_runs_outside_project(tmp_path):
    spec = importlib.util.spec_from_file_location('pair_update_build', ROOT / 'scripts/deployment/Build-Update.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    result = builder.build(output=tmp_path / 'releases')
    import zipfile
    with zipfile.ZipFile(result['zip']) as packed:
        assert packed.testzip() is None
        manifest = json.loads(packed.read('manifest.json'))
        for row in manifest['files']:
            assert hashlib.sha256(packed.read('payload/' + row['path'])).hexdigest() == row['sha256']
        packed.extractall(tmp_path / 'unpacked with spaces')
        assert all(not p.endswith('.local.json') for p in packed.namelist())
    payload = tmp_path / 'unpacked with spaces/payload'
    for entry in ('Install-Qwen38', 'Install-ExtraModels'):
        assert (payload / (entry + '.cmd')).is_file()
        process = subprocess.run([sys.executable, str(payload / f'scripts/inference/{entry}.py'), '--help'],
                                 cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        assert process.returncode == 0, process.stderr
        assert '--no-download' in process.stdout
    if os.name == 'nt' and Path('C:/ProgramData/anaconda3/envs/trt_test_1013/python.exe').is_file():
        for entry in ('Install-ExtraModels', 'Install-GptOss20B', 'Install-Gemma4-12B', 'Install-Gemma4-26B'):
            process = subprocess.run([os.environ['COMSPEC'], '/d', '/c', str(payload / (entry + '.cmd')), '--help'],
                                     cwd=tmp_path, input=b'\n', capture_output=True, timeout=30)
            assert process.returncode == 0, (process.stdout + process.stderr).decode('utf-8', 'replace')
            assert b'--output-tokens' in process.stdout
    for key in installer.MODEL_KEYS:
        source = json.loads((payload / f'config/models/{key}.source.json').read_bytes())
        raw, items = installer.manifest_info(source)
        assert items and hashlib.sha256(raw).hexdigest() == source['manifest_sha256']


def test_five_models_preserve_primary_and_each_existing_registration(root_dir, monkeypatch):
    server, _ = setup_tree(root_dir)
    original = (root_dir / 'config/original.json').read_bytes()
    monkeypatch.setattr(installer.legacy, 'offline', lambda *args: nullcontext())
    monkeypatch.setattr(installer, 'download_model', Mock())
    for key in installer.MODEL_KEYS:
        installer.install(key, root_dir, server_config=server, engine_factory=engine_for(root_dir))
    registered = json.loads(server.read_bytes())
    assert registered['inference_config'] == 'config/original.json'
    assert len(registered['additional_inference_configs']) == 4
    assert (root_dir / 'config/original.json').read_bytes() == original
    before = {p: p.read_bytes() for p in [server, *(root_dir / p for p in registered['additional_inference_configs'])]}
    for key in ('gptoss', 'gemma12', 'gemma26'):
        assert installer.install(key, root_dir, server_config=server, engine_factory=engine_for(root_dir))['backup'] is None
    assert all(p.read_bytes() == data for p, data in before.items())


def test_unverified_gptoss_levels_do_not_register(root_dir, monkeypatch):
    server, _ = setup_tree(root_dir)
    original = server.read_bytes()
    monkeypatch.setattr(installer.legacy, 'offline', lambda *args: nullcontext())
    monkeypatch.setattr(installer, 'download_model', Mock())
    with pytest.raises(ValueError, match='thinking levels'):
        installer.install('gptoss', root_dir, server_config=server, engine_factory=engine_for(root_dir, 'thinking'))
    assert server.read_bytes() == original
    assert not (root_dir / 'config/inference.gptoss.local.json').exists()


def test_gemma26_native_parser_metadata_is_hash_checked_and_published_offline(tmp_path):
    source = json.loads((ROOT / 'config/models/gemma26.source.json').read_bytes())
    manifest = json.loads(source['manifest'])
    cfg = json.loads(source['inline_blobs'][manifest['config']['digest']])
    assert cfg['renderer'] == cfg['parser'] == 'gemma4' and cfg['file_type'] == 'Q3_K_M'
    source['inline_blobs'][manifest['config']['digest']] += 'tampered'
    with pytest.raises(ValueError, match='元数据'):
        installer.download_model(tmp_path, {'ModelsPath': 'store'}, source, allow_download=False)
    assert not (tmp_path / 'store').exists()


def test_inline_config_blob_can_be_installed_without_a_registry(tmp_path):
    source, items, blobs = tiny_source()
    source['inline_blobs'] = {items[0]['digest']: blobs[0].decode()}
    origin = tmp_path / 'origin/blobs'
    origin.mkdir(parents=True)
    (origin / items[1]['digest'].replace(':', '-')).write_bytes(blobs[1])
    path = installer.download_model(tmp_path, {'ModelsPath': 'store'}, source, origin.parent, allow_download=False)
    assert path.is_file()
    for item, data in zip(items, blobs):
        assert (tmp_path / 'store/blobs' / item['digest'].replace(':', '-')).read_bytes() == data

@pytest.mark.parametrize('offset', [0, 5, 12])
def test_real_http_download_resumes_and_recovers_complete_partial(tmp_path, monkeypatch, offset):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    source, items, blobs = tiny_source()
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    monkeypatch.setenv('no_proxy', '127.0.0.1')
    requests_seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            item = next(item for item in items if self.path.endswith(item['digest']))
            data = blobs[items.index(item)]
            begin = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
            requests_seen.append((item['digest'], begin))
            self.send_response(206 if begin else 200)
            self.send_header('Content-Length', str(len(data) - begin))
            if begin:
                self.send_header('Content-Range', f'bytes {begin}-{len(data)-1}/{len(data)}')
            self.end_headers()
            self.wfile.write(data[begin:])
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: http.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    source['registry'] = f'http://127.0.0.1:{http.server_port}'
    partial = tmp_path / 'store/blobs' / (items[0]['digest'].replace(':', '-') + '.download')
    partial.parent.mkdir(parents=True)
    if offset:
        partial.write_bytes(blobs[0][:offset])
    try:
        path = installer.download_model(tmp_path, {'ModelsPath': 'store'}, source)
        assert path.read_text() == source['manifest']
        for item, data in zip(items, blobs):
            assert (tmp_path / 'store/blobs' / item['digest'].replace(':', '-')).read_bytes() == data
        if offset == len(blobs[0]):
            assert not any(sha == items[0]['digest'] for sha, begin in requests_seen)
        else:
            assert requests_seen[0] == (items[0]['digest'], offset)
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
