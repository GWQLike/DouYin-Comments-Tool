"""Ordered SQLite upgrades. Migration functions must not commit or use executescript."""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .task_stats import INDEXES as STATS_INDEXES, migrate as migrate_task_stats, trigger_sql


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


def _comment_field_sources(con: sqlite3.Connection) -> None:
    # Legacy content has no reliable source label; preserve it against previews
    # until a full response explicitly supplies the text again.
    con.execute("ALTER TABLE comments ADD COLUMN text_source TEXT NOT NULL DEFAULT 'legacy' CHECK(text_source IN ('legacy','preview','full'))")
    reader = con.cursor()
    reader.row_factory = sqlite3.Row
    candidates = reader.execute("""SELECT c.* FROM comments c LEFT JOIN authors a
        ON a.task_id=c.task_id AND a.author_key=c.author_key
        WHERE c.author_key='comment:'||c.comment_id AND
            (COALESCE(c.uid,'')!='' OR COALESCE(c.sec_uid,'')!=''
                OR COALESCE(a.uid,'')!='' OR COALESCE(a.sec_uid,'')!='')""").fetchall()
    identities = ('uid', 'sec_uid', 'douyin_id')
    changed_tasks = set()
    for comment in candidates:
        task_id, old_key = comment['task_id'], comment['author_key']
        old = reader.execute('SELECT * FROM authors WHERE task_id=? AND author_key=?', (task_id, old_key)).fetchone()
        records = [comment] + ([old] if old else [])
        fields = {key: next((record[key] for record in records if record[key]), '')
                  for key in (*identities, 'nickname')}
        key = fields['uid'] or fields['sec_uid']
        target = reader.execute('SELECT * FROM authors WHERE task_id=? AND author_key=?', (task_id, key)).fetchone()
        if target:
            records.append(target)
        if any(record['identity_state'] == 'conflict' for record in records) or any(
            len({record[field] for record in records if record[field]}) > 1 for field in identities):
            # Keep ambiguous links and their evidence for individual verification.
            con.execute("UPDATE comments SET identity_state='conflict' WHERE task_id=? AND comment_id=?", (task_id, comment['comment_id']))
            con.execute('''INSERT INTO authors(task_id,author_key,uid,sec_uid,douyin_id,nickname,identity_state)
                VALUES(?,?,?,?,?,?,'conflict') ON CONFLICT(task_id,author_key) DO UPDATE SET identity_state='conflict' ''',
                (task_id, old_key, *(fields[field] for field in (*identities, 'nickname'))))
            changed_tasks.add(task_id)
            continue
        if target:
            fields = {field: target[field] or fields[field] for field in fields}
        state = 'verified' if fields['douyin_id'] else 'unknown'
        con.execute('''INSERT INTO authors(task_id,author_key,uid,sec_uid,douyin_id,nickname,identity_state)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,author_key) DO UPDATE SET
            uid=excluded.uid,sec_uid=excluded.sec_uid,douyin_id=excluded.douyin_id,
            nickname=excluded.nickname,identity_state=excluded.identity_state''',
            (task_id, key, *(fields[field] for field in (*identities, 'nickname')), state))
        con.execute('UPDATE comments SET author_key=? WHERE task_id=? AND comment_id=?', (key, task_id, comment['comment_id']))
        changed_tasks.add(task_id)
    now = datetime.now(timezone.utc).timestamp()
    con.executemany('UPDATE tasks SET updated_at=? WHERE id=?', ((now, task_id) for task_id in changed_tasks))
    # Retain orphaned rows as history. Counts and verification queues only use
    # authors referenced by current comments; lost fields are not reconstructed.


def _author_verification_records(con: sqlite3.Connection) -> None:
    con.execute('''CREATE TABLE author_verifications (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, record_id TEXT NOT NULL,
        author_key TEXT NOT NULL, source_uid TEXT NOT NULL DEFAULT '', source_sec_uid TEXT NOT NULL DEFAULT '',
        source_number TEXT NOT NULL DEFAULT '', source_conflict INTEGER NOT NULL DEFAULT 0,
        uid TEXT NOT NULL DEFAULT '', sec_uid TEXT NOT NULL DEFAULT '', douyin_id TEXT NOT NULL DEFAULT '',
        nickname TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','confirmed','no_query','not_provided','request_failed','conflict')),
        failure_kind TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        last_attempt_at REAL, PRIMARY KEY(task_id,record_id))''')
    # Keep raw comment/video fields intact. A legacy shared author may only
    # supply a result when its IDs agree with this particular comment.
    con.execute('''INSERT INTO author_verifications(task_id,record_id,author_key,source_uid,source_sec_uid,
        source_number,source_conflict,uid,sec_uid,douyin_id,nickname,status,failure_kind)
        SELECT c.task_id,c.comment_id,c.author_key,COALESCE(c.uid,''),
            COALESCE(c.sec_uid,''),COALESCE(c.douyin_id,''),
            CASE WHEN c.identity_state='conflict' OR a.identity_state='conflict' THEN 1 ELSE 0 END,
            COALESCE(NULLIF(c.uid,''),a.uid,''),COALESCE(NULLIF(c.sec_uid,''),a.sec_uid,''),
            COALESCE(NULLIF(a.douyin_id,''),c.douyin_id,''),COALESCE(NULLIF(c.nickname,''),a.nickname,''),
            CASE WHEN c.identity_state='conflict' OR a.identity_state='conflict' THEN 'conflict'
                WHEN COALESCE(NULLIF(a.douyin_id,''),c.douyin_id,'')!='' THEN 'confirmed' ELSE 'pending' END,
            CASE WHEN c.identity_state='conflict' OR a.identity_state='conflict' THEN 'identity_conflict' ELSE '' END
        FROM comments c LEFT JOIN authors a ON a.task_id=c.task_id AND a.author_key=c.author_key
            AND NOT (COALESCE(c.uid,'')!='' AND COALESCE(a.uid,'')!='' AND c.uid!=a.uid)
            AND NOT (COALESCE(c.sec_uid,'')!='' AND COALESCE(a.sec_uid,'')!='' AND c.sec_uid!=a.sec_uid)''')
    con.execute('''INSERT INTO author_verifications(task_id,record_id,author_key,source_uid,source_sec_uid,
        source_number,uid,sec_uid,douyin_id,nickname,status)
        SELECT task_id,video_id,author_key,COALESCE(uid,''),COALESCE(sec_uid,''),COALESCE(author_douyin_id,''),
            COALESCE(uid,''),COALESCE(sec_uid,''),COALESCE(author_douyin_id,''),COALESCE(author_name,''),
            CASE WHEN COALESCE(author_douyin_id,'')!='' THEN 'confirmed' ELSE 'pending' END FROM liked_videos''')
    con.execute('''UPDATE author_verifications AS v SET source_conflict=1,status='conflict',failure_kind='identity_conflict'
        WHERE EXISTS(SELECT 1 FROM liked_videos other WHERE other.task_id=v.task_id AND other.author_key=v.author_key
            AND ((other.uid!='' AND v.source_uid!='' AND other.uid!=v.source_uid)
                OR (other.sec_uid!='' AND v.source_sec_uid!='' AND other.sec_uid!=v.source_sec_uid)))''')


# Append new versions; never rewrite an applied migration to upgrade existing data.
MIGRATIONS = (Migration(1, _adopt_legacy_schema), Migration(2, _reply_observations),
              Migration(3, _comment_field_sources), Migration(4, _author_verification_records),
              Migration(5, migrate_task_stats))

_REQUIRED_COLUMNS = {
    'tasks': 'id source_url video_id title status created_at updated_at error stop_requested '
             'collection_complete verification_complete verify_authors kind target_account '
             'target_sec_uid target_nickname visibility max_videos start_time end_time list_order',
    'comments': 'task_id comment_id video_id parent_id text created_at ip_label nickname '
                'douyin_id uid sec_uid author_key identity_state reply_count text_source',
    'authors': 'task_id author_key uid sec_uid douyin_id nickname identity_state',
    'checkpoints': 'task_id thread_id cursor done restarts reply_count_seen reply_rows_seen',
    'session': 'id status error',
    'liked_videos': 'task_id video_id title author_name author_douyin_id created_at video_url '
                    'author_key uid sec_uid action_at collected_order',
    'author_verifications': 'task_id record_id author_key source_uid source_sec_uid source_number source_conflict '
                           'uid sec_uid douyin_id nickname status failure_kind attempts last_attempt_at',
    'task_stats': 'task_id main_count reply_count video_count missing_times unknown_authors result_version',
    'verification_groups': 'task_id author_key source_uid source_sec_uid pending_records',
}
_PRIMARY_KEYS = {
    'tasks': ['id'], 'comments': ['task_id', 'comment_id'],
    'authors': ['task_id', 'author_key'], 'checkpoints': ['task_id', 'thread_id'],
    'session': ['id'], 'liked_videos': ['task_id', 'video_id'],
    'author_verifications': ['task_id', 'record_id'],
    'task_stats': ['task_id'],
    'verification_groups': ['task_id', 'author_key', 'source_uid', 'source_sec_uid'],
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
    for name, (table, columns) in {**_INDEXES, **_REPLY_INDEXES, **STATS_INDEXES}.items():
        actual = con.execute(f'PRAGMA index_info({name})').fetchall()
        expected = [column.strip().split()[0] for column in columns.split(',')]
        indexes = {row[1] for row in con.execute(f'PRAGMA index_list({table})')}
        if name not in indexes or [row[2] for row in actual] != expected:
            raise DatabaseMigrationError(f'数据库索引 {name} 不匹配')
    if con.execute('SELECT 1 FROM session WHERE id=1').fetchone() is None:
        raise DatabaseMigrationError('数据库会话记录缺失')
    for name, expected in trigger_sql().items():
        row = con.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or ' '.join(row[0].split()) != ' '.join(expected.split()):
            raise DatabaseMigrationError(f'数据库统计触发器 {name} 不匹配')


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
