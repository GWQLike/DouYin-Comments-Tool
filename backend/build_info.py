"""Content-based build metadata shared by initialization and service startup."""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .version import VersionError, read_app_version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
METADATA = 'frontend/dist/build-info.json'
SCHEMA_VERSION = 1
FIXED_INPUTS = (
    'pyproject.toml', 'uv.lock', 'setup.ps1', 'start.ps1',
    '初始化环境.cmd', '启动工具.cmd',
    'frontend/package.json', 'frontend/package-lock.json',
    'frontend/index.html', 'frontend/vite.config.ts', 'frontend/tsconfig.json',
    'frontend/dist/index.html', 'vendor/patches/manifest.json',
    'vendor/MediaCrawler/main.py', 'vendor/MediaCrawler/var.py',
)
VENDOR_PACKAGES = (
    'base', 'cache', 'cmd_arg', 'config', 'constant', 'database', 'libs',
    'media_downloader', 'media_platform', 'model', 'proxy', 'store', 'tools',
)
SOURCE_TREES = (
    ('backend', frozenset({'.py'})),
    ('frontend/src', None),
    ('frontend/dist', None),
    *((f'vendor/MediaCrawler/{name}', frozenset({'.py', '.js'})) for name in VENDOR_PACKAGES),
)
SKIP_DIRS = frozenset({'__pycache__', 'node_modules', 'tests', 'test', 'docs'})


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildInfo:
    app_version: str
    build_id: str


def _checked_path(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink() or path.is_junction():
            raise BuildError('构建输入含链接目录或文件，请使用完整项目副本重新初始化。')
    return path


def _input_paths(root):
    names = set(FIXED_INPUTS)
    for relative, suffixes in SOURCE_TREES:
        tree = _checked_path(root, relative)
        if not tree.is_dir():
            raise BuildError('构建输入目录缺失，请使用完整项目并运行初始化环境.cmd。')
        def walk_error(error):
            raise error
        for directory, folders, files in os.walk(tree, onerror=walk_error, followlinks=False):
            folders[:] = sorted(name for name in folders
                                if not name.startswith('.') and name not in SKIP_DIRS)
            for name in folders:
                _checked_path(root, (Path(directory) / name).relative_to(root))
            for name in files:
                relative_name = (Path(directory) / name).relative_to(root).as_posix()
                if (not name.startswith('.') and relative_name != METADATA
                        and (suffixes is None or Path(name).suffix in suffixes)):
                    names.add(relative_name)
    return sorted(names)


def calculate_build_info(root=None):
    """Hash a stable relative-path manifest, without importing the crawler."""
    root = Path(PROJECT_ROOT if root is None else root).resolve()
    try:
        app_version = read_app_version(root / 'pyproject.toml')
        manifest = []
        for relative in _input_paths(root):
            path = _checked_path(root, relative)
            with path.open('rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise BuildError('构建输入必须是普通文件，请检查项目副本后重新初始化。')
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            manifest.append([relative, digest])
        payload = dict(schema_version=SCHEMA_VERSION, app_version=app_version, files=manifest)
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        return BuildInfo(app_version, hashlib.sha256(encoded).hexdigest())
    except OSError:
        raise BuildError('构建输入缺失或无法读取，请检查项目文件并运行初始化环境.cmd。') from None


def write_build_info(root=None):
    root = Path(PROJECT_ROOT if root is None else root).resolve()
    temporary = None
    try:
        info = calculate_build_info(root)
        target = _checked_path(root, METADATA)
        payload = dict(schema_version=SCHEMA_VERSION, **asdict(info))
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         prefix='.build-info-', suffix='.tmp',
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        return info
    except OSError:
        raise BuildError('无法保存构建信息，请检查前端产物目录与写入权限后重新初始化。') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Only our hidden temporary file can remain; it is never an input.
                pass


def read_build_info(root=None):
    """Validate on each new startup; callers retain the immutable result."""
    root = Path(PROJECT_ROOT if root is None else root).resolve()
    try:
        with _checked_path(root, METADATA).open('rb') as stream:
            content = stream.read(4097)
        if len(content) > 4096:
            raise ValueError('oversized metadata')
        saved = json.loads(content)
        if (not isinstance(saved, dict) or set(saved) != {'schema_version', 'app_version', 'build_id'}
                or type(saved['schema_version']) is not int or saved['schema_version'] != SCHEMA_VERSION
                or not isinstance(saved['app_version'], str) or not isinstance(saved['build_id'], str)
                or re.fullmatch('[0-9a-f]{64}', saved['build_id']) is None):
            raise ValueError('invalid metadata')
    except FileNotFoundError:
        raise BuildError('缺少构建信息，请运行初始化环境.cmd 后重新启动。') from None
    except (ValueError, UnicodeError, OSError):
        raise BuildError('构建信息无效或无法读取，请运行初始化环境.cmd 后重新启动。') from None
    current = calculate_build_info(root)
    if saved['app_version'] != current.app_version or saved['build_id'] != current.build_id:
        raise BuildError('项目文件或前端产物与构建信息不一致，请重新初始化并正常重启工具。')
    return current


def main():
    parser = argparse.ArgumentParser(description='生成或校验应用构建信息')
    parser.add_argument('action', choices=('write', 'check'))
    args = parser.parse_args()
    try:
        info = write_build_info() if args.action == 'write' else read_build_info()
    except (BuildError, VersionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(info.build_id)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
