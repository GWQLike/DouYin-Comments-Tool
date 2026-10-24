"""Check saved counters offline; rebuild only when explicitly requested."""
import argparse
import os
from pathlib import Path

from .db import Database
from .runtime_control import ProcessLease, OwnershipError


def main():
    parser = argparse.ArgumentParser(description='校验或重建本地任务统计')
    default_dir = Path(os.environ.get('DOUYIN_DATA_DIR', Path(__file__).resolve().parents[1] / 'data'))
    parser.add_argument('--db', type=Path, default=default_dir / 'comments.sqlite3')
    parser.add_argument('--rebuild', action='store_true')
    args = parser.parse_args()
    path = args.db.resolve()
    if not path.is_file():
        parser.error('数据库不存在，请先确认路径')
    try:
        with ProcessLease(path.parent, 'service'), ProcessLease(path.parent, 'worker'):
            db = Database(path)
            if args.rebuild:
                count = db.rebuild_statistics()
                print(f'已重建 {count} 个任务的统计，结果版本已更新。')
            differences = db.check_statistics()
            print(f'统计检查完成，不一致任务：{len(differences)}')
            return 1 if differences else 0
    except OwnershipError as exc:
        print(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
