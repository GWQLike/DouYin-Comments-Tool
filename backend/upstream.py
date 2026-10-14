"""Normal imports of the pinned, explicitly patched MediaCrawler package."""
import importlib
import logging
import sys
import hashlib
import json
from functools import lru_cache
from pathlib import Path

VENDOR_ROOT = Path(__file__).resolve().parents[1] / 'vendor' / 'MediaCrawler'
ENGINE_VERSION = '5d547f45862ca2d4b404e5a51775879d579b29d4+workbench.1'


@lru_cache(maxsize=1)
def verify_patch():
    manifest = json.loads((VENDOR_ROOT.parent / 'patches/manifest.json').read_text(encoding='utf-8'))
    if manifest['upstream_commit'] + '+' + manifest['patch_version'] != ENGINE_VERSION:
        raise RuntimeError('MediaCrawler compatibility version mismatch')
    for name, hashes in manifest['files'].items():
        path = (VENDOR_ROOT / name).resolve()
        if not path.is_relative_to(VENDOR_ROOT.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != hashes['patched_sha256']:
            raise RuntimeError('MediaCrawler compatibility patch integrity check failed')


def module(name):
    verify_patch()
    path = str(VENDOR_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)
    # Upstream diagnostics may include full response bodies; host diagnostics
    # provide a safe fixed vocabulary instead.
    logging.getLogger('MediaCrawler').disabled = True
    result = importlib.import_module(name)
    logging.getLogger('MediaCrawler').disabled = True
    return result
