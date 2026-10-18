"""Generate a reviewable patch and integrity manifest from preserved baselines."""
from pathlib import Path
import difflib
import hashlib
import json

root = Path(__file__).resolve().parent
vendor = root.parent / 'MediaCrawler'
names = sorted(path.relative_to(root / 'baseline').as_posix() for path in (root / 'baseline').rglob('*.py'))
names.append('media_platform/douyin/pagination.py')
entries, patches = {}, []
for name in names:
    baseline, patched = root / 'baseline' / name, vendor / name
    old = baseline.read_bytes() if baseline.exists() else b''
    new = patched.read_bytes()
    entries[name] = {'baseline_sha256': hashlib.sha256(old).hexdigest() if old else None,
                     'patched_sha256': hashlib.sha256(new).hexdigest()}
    patches.extend(difflib.unified_diff(old.decode('utf-8-sig').splitlines(keepends=True),
        new.decode('utf-8-sig').splitlines(keepends=True),
        fromfile='a/' + name if old else '/dev/null', tofile='b/' + name))
(root / 'workbench-2.patch').write_text(''.join(patches), encoding='utf-8', newline='\n')
(root / 'manifest.json').write_text(json.dumps({'upstream_commit': '5d547f45862ca2d4b404e5a51775879d579b29d4',
    'patch_version': 'workbench.2', 'files': entries}, indent=2), encoding='utf-8')
print(f'Compatibility patch recorded: {len(entries)} files')
