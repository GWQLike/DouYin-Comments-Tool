"""Validate bundle identity independently of Git, timestamps and user data."""
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend import app as app_module, build_info, version


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    files = {
        'pyproject.toml': '[project]\nversion = "0.1.0"\n',
        'uv.lock': 'version = 1\n', 'setup.ps1': 'setup', 'start.ps1': 'start',
        '初始化环境.cmd': 'setup.cmd', '启动工具.cmd': 'start.cmd',
        'frontend/package.json': '{}', 'frontend/package-lock.json': '{}',
        'frontend/index.html': '<html>source</html>', 'frontend/vite.config.ts': 'config',
        'frontend/tsconfig.json': '{}', 'frontend/src/main.tsx': 'source',
        'frontend/src/image.png': 'image bytes', 'frontend/dist/index.html': '<html>bundle</html>',
        'frontend/dist/assets/main.js': 'bundle', 'vendor/patches/manifest.json': '{}',
        'vendor/MediaCrawler/main.py': 'main', 'vendor/MediaCrawler/var.py': 'variables',
        'backend/app.py': 'app', 'backend/version.py': 'version', 'backend/build_info.py': 'build',
        'vendor/MediaCrawler/libs/douyin.js': 'signature',
        'vendor/MediaCrawler/media_platform/douyin/core.py': 'crawler',
    }
    for name in ('base', 'cache', 'cmd_arg', 'config', 'constant', 'database',
                 'media_downloader', 'media_platform', 'model', 'proxy', 'store', 'tools'):
        files[f'vendor/MediaCrawler/{name}/__init__.py'] = name
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
    return root, files


def test_digest_matches_independent_relative_path_manifest(project):
    root, files = project
    payload = {'schema_version': 1, 'app_version': '0.1.0', 'files': [
        [name, hashlib.sha256(files[name].encode('utf-8')).hexdigest()] for name in sorted(files)
    ]}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=True).encode('ascii')).hexdigest()
    assert build_info.calculate_build_info(root).build_id == expected


def test_repeat_build_copy_timestamps_and_local_files_do_not_change_id(project, tmp_path, monkeypatch):
    root, files = project
    before = build_info.write_build_info(root)
    saved_bytes = (root / build_info.METADATA).read_bytes()
    assert build_info.write_build_info(root) == before
    assert (root / build_info.METADATA).read_bytes() == saved_bytes
    for name in ('data/cookies.json', '.git/config', '.env', 'tests/test_api.py',
                 'backend/__pycache__/app.pyc', 'frontend/node_modules/runtime.js',
                 'vendor/MediaCrawler/docs/example.py', 'vendor/MediaCrawler/.env',
                 'frontend/dist/.build-info-interrupted.tmp'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture-private-cookie-value', encoding='utf-8')
    monkeypatch.setenv('DOUYIN_DATA_DIR', 'fixture-private-data-path')
    for relative in files:
        os.utime(root / relative, (1000, 1000))
    assert build_info.read_build_info(root) == before
    copy = tmp_path / 'copy-without-git'
    for relative in files:
        target = copy / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    assert build_info.write_build_info(copy) == before
    assert not (copy / '.git').exists()
    assert json.loads(saved_bytes) == {'schema_version': 1, 'app_version': '0.1.0', 'build_id': before.build_id}
    assert str(root) not in saved_bytes.decode() and 'fixture-private' not in saved_bytes.decode()


@pytest.mark.parametrize('relative', [
    'backend/app.py', 'backend/new_module.py', 'vendor/MediaCrawler/libs/douyin.js',
    'vendor/MediaCrawler/media_platform/douyin/core.py', 'vendor/patches/manifest.json',
    'uv.lock', 'setup.ps1', 'start.ps1', '启动工具.cmd', '初始化环境.cmd',
    'frontend/package.json', 'frontend/package-lock.json', 'frontend/vite.config.ts',
    'frontend/src/main.tsx', 'frontend/src/image.png', 'frontend/dist/assets/main.js',
    'frontend/dist/assets/new.css',
])
def test_changed_or_added_input_invalidates_saved_build(project, relative):
    root, _ = project
    previous = build_info.write_build_info(root)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('changed', encoding='utf-8')
    with pytest.raises(build_info.BuildError, match='不一致'):
        build_info.read_build_info(root)
    assert build_info.write_build_info(root).build_id != previous.build_id


def test_removed_input_and_missing_required_files_fail(project):
    root, _ = project
    build_info.write_build_info(root)
    (root / 'frontend/src/image.png').unlink()
    with pytest.raises(build_info.BuildError, match='不一致'):
        build_info.read_build_info(root)
    (root / 'frontend/dist/index.html').unlink()
    with pytest.raises(build_info.BuildError, match='构建输入缺失'):
        build_info.write_build_info(root)


@pytest.mark.parametrize('payload', [
    b'{', b'\xff', b'[]', b'null', b'{}', b'"fixture-private-value"', b'x' * 4097,
    json.dumps({'schema_version': True, 'app_version': '0.1.0', 'build_id': 'a' * 64}).encode(),
    json.dumps({'schema_version': 2, 'app_version': '0.1.0', 'build_id': 'a' * 64}).encode(),
    json.dumps({'schema_version': 1, 'app_version': '0.1.0', 'build_id': '../bad'}).encode(),
])
def test_invalid_metadata_is_a_safe_controlled_error(project, payload):
    root, _ = project
    build_info.write_build_info(root)
    (root / build_info.METADATA).write_bytes(payload)
    with pytest.raises(build_info.BuildError, match='构建信息无效') as error:
        build_info.read_build_info(root)
    assert 'fixture-private' not in str(error.value) and str(root) not in str(error.value)


def test_missing_metadata_does_not_fall_back_to_previous_read(project):
    root, _ = project
    build_info.write_build_info(root)
    build_info.read_build_info(root)
    (root / build_info.METADATA).unlink()
    with pytest.raises(build_info.BuildError, match='缺少构建信息'):
        build_info.read_build_info(root)


def test_failed_atomic_replace_keeps_previous_metadata_and_cleans_own_temp(project, monkeypatch):
    root, _ = project
    build_info.write_build_info(root)
    saved = (root / build_info.METADATA).read_bytes()
    def denied(*_):
        raise PermissionError('fixture-private-path')
    monkeypatch.setattr(build_info.os, 'replace', denied)
    with pytest.raises(build_info.BuildError, match='无法保存') as error:
        build_info.write_build_info(root)
    assert 'fixture-private-path' not in str(error.value)
    assert (root / build_info.METADATA).read_bytes() == saved
    assert not list((root / 'frontend/dist').glob('.build-info-*.tmp'))


def test_linked_input_directory_is_rejected(project, monkeypatch):
    root, _ = project
    original = Path.is_junction
    linked = root / 'vendor/MediaCrawler/libs'
    monkeypatch.setattr(Path, 'is_junction', lambda path: path == linked or original(path))
    with pytest.raises(build_info.BuildError, match='链接'):
        build_info.write_build_info(root)


def test_symlinked_input_cannot_read_outside_project(project, tmp_path):
    root, _ = project
    outside = tmp_path / 'private.py'
    outside.write_text('fixture-private-value', encoding='utf-8')
    path = root / 'backend/link.py'
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip('Creating symlinks requires Windows developer mode or privilege')
    with pytest.raises(build_info.BuildError, match='链接'):
        build_info.calculate_build_info(root)


def test_service_keeps_immutable_startup_snapshot_and_next_start_checks_disk(project, tmp_path, monkeypatch):
    root, _ = project
    monkeypatch.setattr(app_module, 'ROOT', root)
    monkeypatch.setattr(version, 'PROJECT_FILE', root / 'pyproject.toml')
    before = build_info.write_build_info(root)
    def service(directory):
        return app_module.create_app(directory, build_loader=build_info.read_build_info,
                                     manager_factory=lambda *_: SimpleNamespace(stop=lambda: None))
    app = service(tmp_path / 'service1')
    with TestClient(app) as client:
        assert app.state.build_info == before
        with pytest.raises(FrozenInstanceError):
            app.state.build_info.build_id = 'changed'
        (root / 'frontend/dist/assets/main.js').write_text('new bundle', encoding='utf-8')
        assert client.get('/api/health').json() == {'status': 'ok', 'app': 'douyin-comments'}
        assert app.state.build_info == before
        second = tmp_path / 'service2'
        with pytest.raises(build_info.BuildError, match='不一致'):
            with TestClient(service(second)):
                pytest.fail('Stale build was accepted')
        assert not second.exists()
        after = build_info.write_build_info(root)
        assert after != before and app.state.build_info == before
        with TestClient(service(second)) as new_client:
            assert new_client.app.state.build_info == after
    with TestClient(app) as restarted:
        assert restarted.app.state.build_info == after


def test_deployed_app_requires_build_metadata(project, monkeypatch):
    root, _ = project
    monkeypatch.setattr(app_module, 'ROOT', root)
    with pytest.raises(build_info.BuildError, match='缺少构建信息'):
        with TestClient(app_module.app):
            pytest.fail('The deployed module app must validate its bundle')


def test_app_version_cannot_change_between_creation_and_start(project, tmp_path, monkeypatch):
    root, _ = project
    monkeypatch.setattr(app_module, 'ROOT', root)
    monkeypatch.setattr(version, 'PROJECT_FILE', root / 'pyproject.toml')
    app = app_module.create_app(tmp_path / 'service', build_loader=build_info.read_build_info)
    (root / 'pyproject.toml').write_text('[project]\nversion = "0.2.0"', encoding='utf-8')
    build_info.write_build_info(root)
    with pytest.raises(build_info.BuildError, match='版本在启动前发生变化'):
        with TestClient(app):
            pytest.fail('App version and bundle version diverged')
    assert not (tmp_path / 'service').exists()


def test_real_cli_from_other_directory_and_safe_failure_without_git(project, tmp_path):
    root, _ = project
    for module in (build_info, version):
        shutil.copyfile(module.__file__, root / 'backend' / Path(module.__file__).name)
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONPATH': str(root)}
    def invoke(action):
        return subprocess.run([sys.executable, '-m', 'backend.build_info', action], cwd=tmp_path,
                              env=env, capture_output=True, text=True, encoding='utf-8', timeout=15)
    written = invoke('write')
    checked = invoke('check')
    assert written.returncode == checked.returncode == 0, written.stderr + checked.stderr
    assert written.stdout == checked.stdout and len(checked.stdout.strip()) == 64
    assert not written.stderr + checked.stderr
    (root / 'backend/app.py').write_text('new source', encoding='utf-8')
    stale = invoke('check')
    assert stale.returncode == 1 and not stale.stdout and '不一致' in stale.stderr
    assert str(root) not in stale.stderr and 'Traceback' not in stale.stderr


def test_reader_does_not_import_crawler_or_database(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', '''
import sys
import backend.build_info
assert not any(name in sys.modules for name in (
    'backend.app', 'backend.db', 'backend.engine', 'backend.upstream', 'fastapi'))
'''], cwd=tmp_path, env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONPATH': str(root)},
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
