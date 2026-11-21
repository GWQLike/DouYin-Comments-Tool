"""Application metadata must be shared by the CLI and each new service."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend import version
from backend.app import create_app


@pytest.mark.parametrize('value', [
    '0.1.0', '2', '2.3', '2.0rc1', '2.0.post1', '2.0.dev1',
    '1!2.0rc1.post2.dev3+local.4', ' 2.0+release.1 ',
])
def test_reads_declared_project_version(tmp_path, value):
    source = tmp_path / 'pyproject.toml'
    source.write_text(f'[project]\nversion = "{value}"\n', encoding='utf-8')
    assert version.read_app_version(source) == value.strip()


@pytest.mark.parametrize('metadata', [
    '', '[tool.example]\nversion = "1.0"', '[project]\nname = "example"',
    'project = []', '[project]\nversion = 1', '[project]\nversion = true',
    '[project]\nversion = []', '[project]\nversion = ""',
    '[project]\nversion = "latest"', '[project]\nversion = "1.2.3suffix"',
    '[project]\nversion = "1.2..3"', '[project]\nversion = "1.2+"',
])
def test_rejects_missing_or_invalid_version(tmp_path, metadata):
    source = tmp_path / 'pyproject.toml'
    source.write_text(metadata, encoding='utf-8')
    with pytest.raises(version.VersionError, match=r'\[project\].version'):
        version.read_app_version(source)


@pytest.mark.parametrize('content', [b'[project\n', b'\xff\xfe'])
def test_rejects_invalid_metadata_encoding_or_syntax(tmp_path, content):
    source = tmp_path / 'pyproject.toml'
    source.write_bytes(content)
    with pytest.raises(version.VersionError, match='格式无效') as error:
        version.read_app_version(source)
    assert str(tmp_path) not in str(error.value)
    assert error.value.__suppress_context__


def test_missing_and_unreadable_sources_fail_explicitly(tmp_path):
    source = tmp_path / 'pyproject.toml'
    with pytest.raises(version.VersionError, match='缺少 pyproject.toml'):
        version.read_app_version(source)
    source.mkdir()
    with pytest.raises(version.VersionError, match='读取权限'):
        version.read_app_version(source)


def test_reads_do_not_fall_back_to_a_previous_version(tmp_path, monkeypatch):
    source = tmp_path / 'pyproject.toml'
    monkeypatch.setattr(version, 'PROJECT_FILE', source)
    source.write_text('[project]\nversion = "1.2.3"', encoding='utf-8')
    assert version.read_app_version() == '1.2.3'
    source.write_text('[project]\nversion = "fixture-private-value"', encoding='utf-8')
    with pytest.raises(version.VersionError) as error:
        version.read_app_version()
    assert 'fixture-private-value' not in str(error.value)
    source.unlink()
    with pytest.raises(version.VersionError, match='缺少'):
        version.read_app_version()


def test_each_app_uses_its_creation_version_for_openapi(tmp_path, monkeypatch):
    source = tmp_path / 'pyproject.toml'
    monkeypatch.setattr(version, 'PROJECT_FILE', source)
    apps = []
    for index, declared in enumerate(['1.2.3', '1.2.4rc1']):
        source.write_text(f'[project]\nversion = "{declared}"', encoding='utf-8')
        app = create_app(tmp_path / str(index),
                         manager_factory=lambda *_: SimpleNamespace(stop=lambda: None))
        apps.append((app, declared))
    # Even an unreadable source cannot make an existing service report a new version.
    source.unlink()
    for app, expected in apps:
        with TestClient(app) as client:
            assert app.version == expected
            assert client.get('/openapi.json').json()['info']['version'] == expected
            assert client.get('/api/health').json() == {'status': 'ok', 'app': 'douyin-comments'}


@pytest.mark.parametrize('content', [None, '[project]\nversion = "invalid"'])
def test_app_rejects_bad_metadata_before_creating_data(tmp_path, monkeypatch, content):
    source = tmp_path / 'pyproject.toml'
    if content is not None:
        source.write_text(content, encoding='utf-8')
    monkeypatch.setattr(version, 'PROJECT_FILE', source)
    data_dir = tmp_path / 'not-created'
    with pytest.raises(version.VersionError):
        create_app(data_dir)
    assert not data_dir.exists()


@pytest.mark.parametrize('content,success,expected', [
    ('[project]\nversion = "3.4.5"', True, '3.4.5'),
    (None, False, '缺少 pyproject.toml'),
    ('[project]\nversion = "fixture-private-value"', False, '[project].version'),
    ('[project\n', False, '格式无效'),
])
def test_cli_works_without_git_or_installed_project_metadata(tmp_path, content, success, expected):
    package = tmp_path / 'backend'
    package.mkdir()
    shutil.copyfile(version.__file__, package / 'version.py')
    if content is not None:
        (tmp_path / 'pyproject.toml').write_text(content, encoding='utf-8')
    env = {**os.environ, 'PYTHONUTF8': '1'}
    env.pop('PYTHONPATH', None)
    result = subprocess.run(
        [sys.executable, '-m', 'backend.version'], cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert result.returncode == (0 if success else 1), result.stderr
    if success:
        assert result.stdout.strip() == expected and not result.stderr
    else:
        assert not result.stdout and expected in result.stderr
        assert 'fixture-private-value' not in result.stderr
        assert str(tmp_path) not in result.stderr and 'Traceback' not in result.stderr


def test_lightweight_reader_resolves_source_independently_of_working_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-c', '''
import sys
from backend.version import read_app_version
print(read_app_version())
assert not any(name in sys.modules for name in (
    'backend.app', 'backend.db', 'backend.engine', 'backend.upstream', 'fastapi'))
'''], cwd=tmp_path, env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONPATH': str(root)},
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == version.read_app_version() and not result.stderr
