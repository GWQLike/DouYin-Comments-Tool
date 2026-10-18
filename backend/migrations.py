"""Ordered SQLite upgrades. Migration functions must not commit or use executescript."""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class DatabaseMigrationError(RuntimeError):
    """Initialization failed; callers must not start services against this database."""

    def __init__(self, message: str, backup_path: Path | None = None):
        super().__init__(message)
        self.backup_path = backup_path


@dataclass(frozen=True)
class Migration:
    version: int
    apply: Callable[[sqlite3.Connection], None]


_BASE_TABLES = (
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, source_url TEXT NOT NULL, video_id TEXT DEFAULT '',
        title TEXT DEFAULT '', status TEXT DEFAULT 'created', created_at REAL,
        updated_at REAL, error TEXT, stop_requested INTEGER DEFAULT 0,
        collection_complete INTEGER DEFAULT 0, verification_complete INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS comments (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        comment_id TEXT NOT NULL, video_id TEXT NOT NULL, parent_id TEXT DEFAULT '',
        text TEXT DEFAULT '', created_at REAL, ip_label TEXT DEFAULT '',
        nickname TEXT DEFAULT '', douyin_id TEXT DEFAULT '', uid TEXT DEFAULT '',
        sec_uid TEXT DEFAULT '', author_key TEXT NOT NULL,
        identity_state TEXT DEFAULT 'unknown', reply_count INTEGER DEFAULT 0,
        PRIMARY KEY (task_id,comment_id)
    )""",
    """CREATE TABLE IF NOT EXISTS authors (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        author_key TEXT NOT NULL, uid TEXT DEFAULT '', sec_uid TEXT DEFAULT '',
        douyin_id TEXT DEFAULT '', nickname TEXT DEFAULT '', identity_state TEXT DEFAULT 'unknown',
        PRIMARY KEY(task_id,author_key)
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        thread_id TEXT NOT NULL, cursor TEXT DEFAULT '0', done INTEGER DEFAULT 0,
        restarts INTEGER DEFAULT 0, PRIMARY KEY(task_id,thread_id)
    )""",
    'CREATE TABLE IF NOT EXISTS session (id INTEGER PRIMARY KEY CHECK(id=1),status TEXT,error TEXT)',
    """CREATE TABLE IF NOT EXISTS liked_videos (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        video_id TEXT NOT NULL, title TEXT DEFAULT '', author_name TEXT DEFAULT '',
        author_douyin_id TEXT DEFAULT '', created_at REAL, video_url TEXT NOT NULL,
        author_key TEXT NOT NULL, uid TEXT DEFAULT '', sec_uid TEXT DEFAULT '',
        PRIMARY KEY(task_id,video_id)
    )""",
)

_LEGACY_ADDITIONS = {
    'tasks': {
        # Existing tasks retain opt-in; create_task explicitly opts new tasks out.
        'verify_authors': 'INTEGER NOT NULL DEFAULT 1',
        'kind': "TEXT NOT NULL DEFAULT 'comments'",
        'target_account': "TEXT NOT NULL DEFAULT ''",
        'target_sec_uid': "TEXT NOT NULL DEFAULT ''",
        'target_nickname': "TEXT NOT NULL DEFAULT ''",
        'visibility': "TEXT NOT NULL DEFAULT 'unknown'",
        'max_videos': 'INTEGER NOT NULL DEFAULT 0',
        'start_time': 'REAL',
        'end_time': 'REAL',
        'list_order': "TEXT NOT NULL DEFAULT 'publication'",
    },
    'liked_videos': {'action_at': 'REAL', 'collected_order': 'INTEGER'},
}

_INDEXES = {
    'comments_author': ('comments', 'task_id,author_key'),
    'comments_time': ('comments', 'task_id,created_at DESC,comment_id DESC'),
    'likes_time': ('liked_videos', 'task_id,created_at DESC,video_id DESC'),
    'likes_author': ('liked_videos', 'task_id,author_key'),
}


def _adopt_legacy_schema(con: sqlite3.Connection) -> None:
    # Version 0 includes all releases before version tracking was introduced.
    # Conditional additions belong only here; later migrations run once in order.
    for statement in _BASE_TABLES:
        con.execute(statement)
    for table, additions in _LEGACY_ADDITIONS.items():
        columns = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
        for name, declaration in additions.items():
            if name not in columns:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    for name, (table, columns) in _INDEXES.items():
        con.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})')
    con.execute("INSERT OR IGNORE INTO session VALUES(1,'logged_out',NULL)")


_REPLY_INDEXES = {'comments_parent': ('comments', 'task_id,parent_id')}


def _reply_observations(con: sqlite3.Connection) -> None:
    con.execute('ALTER TABLE checkpoints ADD COLUMN reply_count_seen INTEGER')
    con.execute('ALTER TABLE checkpoints ADD COLUMN reply_rows_seen INTEGER')
    for name, (table, columns) in _REPLY_INDEXES.items():
        con.execute(f'CREATE INDEX {name} ON {table}({columns})')
    # Historical completion is preserved. Existing rows supply a conservative
    # baseline, not a claim that the old fetch observed these exact counts.
    con.execute('''UPDATE checkpoints SET
        reply_count_seen=COALESCE((SELECT MAX(0,c.reply_count) FROM comments c
            WHERE c.task_id=checkpoints.task_id AND c.comment_id=checkpoints.thread_id AND c.parent_id=''),0),
        reply_rows_seen=(SELECT COUNT(*) FROM comments c
            WHERE c.task_id=checkpoints.task_id AND c.parent_id=checkpoints.thread_id)
        WHERE thread_id!='' AND task_id IN (SELECT id FROM tasks WHERE kind='comments')''')


# Append new versions; never rewrite an applied migration to upgrade existing data.
MIGRATIONS = (Migration(1, _adopt_legacy_schema), Migration(2, _reply_observations))

_REQUIRED_COLUMNS = {
    'tasks': 'id source_url video_id title status created_at updated_at error stop_requested '
             'collection_complete verification_complete verify_authors kind target_account '
             'target_sec_uid target_nickname visibility max_videos start_time end_time list_order',
    'comments': 'task_id comment_id video_id parent_id text created_at ip_label nickname '
                'douyin_id uid sec_uid author_key identity_state reply_count',
    'authors': 'task_id author_key uid sec_uid douyin_id nickname identity_state',
    'checkpoints': 'task_id thread_id cursor done restarts reply_count_seen reply_rows_seen',
    'session': 'id status error',
    'liked_videos': 'task_id video_id title author_name author_douyin_id created_at video_url '
                    'author_key uid sec_uid action_at collected_order',
}
_PRIMARY_KEYS = {
    'tasks': ['id'], 'comments': ['task_id', 'comment_id'],
    'authors': ['task_id', 'author_key'], 'checkpoints': ['task_id', 'thread_id'],
    'session': ['id'], 'liked_videos': ['task_id', 'video_id'],
}


def _validate_schema(con: sqlite3.Connection) -> None:
    # Do not silently recreate damaged versioned tables during ordinary startup.
    for table, required in _REQUIRED_COLUMNS.items():
        columns = con.execute(f'PRAGMA table_info({table})').fetchall()
        if not set(required.split()) <= {row[1] for row in columns}:
            raise DatabaseMigrationError(f'数据库表 {table} 缺少必要字段')
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if primary != _PRIMARY_KEYS[table]:
            raise DatabaseMigrationError(f'数据库表 {table} 的主键不匹配')
    for name, (table, columns) in {**_INDEXES, **_REPLY_INDEXES}.items():
        actual = con.execute(f'PRAGMA index_info({name})').fetchall()
        expected = [column.strip().split()[0] for column in columns.split(',')]
        indexes = {row[1] for row in con.execute(f'PRAGMA index_list({table})')}
        if name not in indexes or [row[2] for row in actual] != expected:
            raise DatabaseMigrationError(f'数据库索引 {name} 不匹配')
    if con.execute('SELECT 1 FROM session WHERE id=1').fetchone() is None:
        raise DatabaseMigrationError('数据库会话记录缺失')


def _create_backup(path: Path, old_version: int, new_version: int) -> Path:
    root = path.parent / 'backups'
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    # An exclusive directory keeps existing backups intact, also on retry.
    directory = Path(tempfile.mkdtemp(
        prefix=f'{path.name}.v{old_version}-v{new_version}.{stamp}-', dir=root))
    partial = directory / 'backup.incomplete'
    # The caller holds BEGIN IMMEDIATE. Other writers are blocked, but a second
    # read connection can back up the committed state, including WAL pages.
    # Backing up the caller's write connection would wait on its own transaction.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)) as source:
        with closing(sqlite3.connect(partial)) as destination:
            source.backup(destination, pages=256)
            if destination.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise DatabaseMigrationError('升级前备份校验失败，数据库未迁移')
    backup = directory / 'backup.sqlite3'
    partial.rename(backup)
    return backup


def initialize_database(path: Path) -> None:
    """Back up once, apply all pending versions atomically, or abort startup."""
    versions = [migration.version for migration in MIGRATIONS]
    if not versions or versions != list(range(1, len(versions) + 1)):
        raise DatabaseMigrationError('数据库迁移版本必须从 1 开始连续排列')
    target = versions[-1]
    backup = None
    with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as con:
        con.execute('PRAGMA foreign_keys=ON')
        try:
            # Serializes version checks, backups and migrations across processes.
            con.execute('BEGIN IMMEDIATE')
            version = con.execute('PRAGMA user_version').fetchone()[0]
            if version < 0 or version > target:
                raise DatabaseMigrationError(
                    f'数据库版本 {version} 不受当前程序支持（最高版本 {target}）')
            if version < target:
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if version == 0 and tables and 'tasks' not in tables:
                    raise DatabaseMigrationError('无法识别已有数据库')
                if tables:
                    backup = _create_backup(path, version, target)
                for migration in MIGRATIONS:
                    if migration.version > version:
                        migration.apply(con)
                        con.execute(f'PRAGMA user_version={migration.version}')
                _validate_schema(con)
                if con.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise DatabaseMigrationError('迁移后数据库校验失败')
                if con.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise DatabaseMigrationError('迁移后数据库存在无效关联')
            else:
                _validate_schema(con)
            con.execute('COMMIT')
        except BaseException as exc:
            if con.in_transaction:
                con.execute('ROLLBACK')
            if not isinstance(exc, Exception):
                raise
            reason = str(exc) if isinstance(exc, DatabaseMigrationError) else '数据库备份或迁移失败'
            backup_note = f'；升级前备份：{backup}' if backup else ''
            raise DatabaseMigrationError(
                f'{reason}；启动已停止，未提交的迁移已回滚{backup_note}', backup) from exc
        # This pragma cannot run in a transaction. No application code runs until
        # it succeeds; a failure here leaves a fully committed schema, never half an upgrade.
        try:
            mode = con.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if mode.lower() != 'wal':
                raise DatabaseMigrationError('无法启用 SQLite WAL 模式，启动已停止', backup)
        except sqlite3.Error as exc:
            raise DatabaseMigrationError('数据库 WAL 初始化失败，启动已停止', backup) from exc
