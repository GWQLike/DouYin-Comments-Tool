"""Read the application version from the project metadata."""
from pathlib import Path
import re
import sys
import tomllib


PROJECT_FILE = Path(__file__).resolve().parents[1] / 'pyproject.toml'

# PEP 440 release, pre-release, post-release, development and local segments.
VERSION_PATTERN = re.compile(r"""
    v? (?:[0-9]+!)? [0-9]+(?:\.[0-9]+)*
    (?:[._-]?(?:alpha|a|beta|b|preview|pre|c|rc)[._-]?[0-9]*)?
    (?:-[0-9]+|[._-]?(?:post|rev|r)[._-]?[0-9]*)?
    (?:[._-]?dev[._-]?[0-9]*)?
    (?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?
""", re.IGNORECASE | re.VERBOSE | re.ASCII)


class VersionError(RuntimeError):
    pass


def read_app_version(project_file: Path | None = None) -> str:
    source = PROJECT_FILE if project_file is None else project_file
    try:
        with source.open('rb') as stream:
            metadata = tomllib.load(stream)
    except FileNotFoundError:
        raise VersionError('缺少 pyproject.toml，无法读取应用版本，请使用完整项目目录。') from None
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise VersionError('pyproject.toml 格式无效，无法读取应用版本，请检查项目文件。') from None
    except OSError:
        raise VersionError('无法读取 pyproject.toml，请检查项目文件与读取权限。') from None

    project = metadata.get('project')
    version = project.get('version') if isinstance(project, dict) else None
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version.strip()) is None:
        raise VersionError('pyproject.toml 的 [project].version 必须是有效的 Python 版本字符串。')
    return version.strip()


def main() -> int:
    try:
        print(read_app_version())
    except VersionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
