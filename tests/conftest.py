"""Keep the module-level API app away from the user's production database."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory


_bootstrap = None


def pytest_configure(config):
    global _bootstrap
    if 'DOUYIN_DATA_DIR' not in os.environ:
        data = Path(__file__).resolve().parents[1] / 'data'
        data.mkdir(exist_ok=True)
        _bootstrap = TemporaryDirectory(prefix='pytest-app-', dir=data)
        os.environ['DOUYIN_DATA_DIR'] = _bootstrap.name


def pytest_unconfigure(config):
    if _bootstrap is not None:
        os.environ.pop('DOUYIN_DATA_DIR', None)
        _bootstrap.cleanup()
