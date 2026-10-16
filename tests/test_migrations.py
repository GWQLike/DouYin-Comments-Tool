import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from backend import migrations
from backend.db import Database
from backend.migrations import DatabaseMigrationError, Migration


FIXTURE = Path(__file__).parent / 'fixtures' / 'database_v0.sql'


def legacy_database(path):
    with closing(sqlite3.connect(path)) as con:
        con.executescript(FIXTURE.read_text(encoding='utf-8'))


def snapshot(path):
    with closing(sqlite3.connect(path)) as con:
        return con.execute('PRAGMA user_version').fetchone()[0], tuple(con.iterdump())


def backups(path):
    return sorted((path.parent / 'backups').glob('*/backup.sqlite3'))


@pytest.mark.parametrize('initial', ['missing', 'zero_bytes', 'sqlite_empty'])
def test_new_database_needs_no_backup(tmp_path, initial):
    path = tmp_path / 'new.sqlite3'
    if initial == 'zero_bytes':
        path.touch()
    elif initial == 'sqlite_empty':
        with closing(sqlite3.connect(path)) as con:
            con.execute('VACUUM')
    db = Database(path)
    assert snapshot(path)[0] == 1
    assert db.list_tasks() == []
    assert db.session_get() == {'status': 'logged_out', 'error': None}
    assert db.create_task('test')['verify_authors'] is False
    with db.connection() as con:
        assert con.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert con.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    Database(path)
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_upgrade_backs_up_committed_pages_and_preserves_every_record(tmp_path, journal):
    path = tmp_path / '旧库 #1.sqlite3'
    legacy_database(path)
    # Keep a connection alive so WAL commits are not checkpointed on last close.
    with closing(sqlite3.connect(path)) as writer:
        writer.execute(f'PRAGMA journal_mode={journal}')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE tasks SET title='升级前最后一笔提交' WHERE id='comments'")
        writer.commit()
        if journal == 'WAL':
            assert Path(str(path) + '-wal').stat().st_size > 0
        before = snapshot(path)
        db = Database(path)
        saved, = backups(path)
        assert snapshot(saved) == before
        assert snapshot(path) == (1, before[1])
        assert db.get_task('comments')['verify_authors'] is False
        assert db.get_task('likes')['verify_authors'] is True
        assert db.get_task('recommendations')['list_order'] == 'activity'
        assert db.get_checkpoint('comments') == {'cursor': 40, 'done': False, 'restarts': 1}
        assert db.get_checkpoint('comments', 'c1') == {'cursor': 20, 'done': False, 'restarts': 0}
        assert db.read_comments('comments')[0]['text'] == '第一条评论'
        assert db.read_liked_videos('recommendations')[0]['action_at'] == 1700000200
        assert db.session_get()['status'] == 'logged_in'
        for _ in range(3):
            Database(path)
        assert backups(path) == [saved]
        assert snapshot(path) == (1, before[1])


def test_upgrade_of_older_columns_preserves_legacy_defaults(tmp_path):
    path = tmp_path / 'older.sqlite3'
    legacy_database(path)
    with closing(sqlite3.connect(path)) as con:
        for column in ('verify_authors', 'list_order', 'max_videos', 'start_time', 'end_time'):
            con.execute(f'ALTER TABLE tasks DROP COLUMN {column}')
        for column in ('action_at', 'collected_order'):
            con.execute(f'ALTER TABLE liked_videos DROP COLUMN {column}')
    before = snapshot(path)
    db = Database(path)
    assert snapshot(backups(path)[0]) == before
    for task in db.list_tasks():
        assert task['verify_authors'] is True
        assert task['list_order'] == 'publication'
        assert task['max_videos'] == 0
    assert db.get_checkpoint('comments')['cursor'] == 40
    assert len(db.read_comments('comments')) == 2
    assert db.create_task('new')['verify_authors'] is False


def test_ordered_future_migrations_run_once_with_one_backup(tmp_path, monkeypatch):
    path = tmp_path / 'ordered.sqlite3'
    Database(path).create_task('keep')
    before = snapshot(path)
    calls = []

    def version_two(con):
        calls.append(2)
        assert con.execute('PRAGMA user_version').fetchone()[0] == 1
        con.execute('ALTER TABLE tasks ADD COLUMN migration_probe INTEGER DEFAULT 0')
        con.execute('UPDATE tasks SET migration_probe=7')

    def version_three(con):
        calls.append(3)
        assert con.execute('PRAGMA user_version').fetchone()[0] == 2
        assert con.execute('SELECT migration_probe FROM tasks').fetchone()[0] == 7
        con.execute('CREATE INDEX migration_probe_idx ON tasks(migration_probe)')

    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS + (
        Migration(2, version_two), Migration(3, version_three)))
    Database(path)
    assert snapshot(path)[0] == 3
    assert snapshot(backups(path)[0]) == before
    Database(path)
    assert calls == [2, 3]
    assert len(backups(path)) == 1


@pytest.mark.parametrize('failure', ['exception', 'invalid_schema', 'invalid_reference'])
def test_failure_rolls_back_all_versions_ddl_data_and_version(tmp_path, monkeypatch, failure):
    path = tmp_path / 'rollback.sqlite3'
    legacy_database(path)
    before = snapshot(path)
    original = migrations.MIGRATIONS

    def version_two(con):
        con.execute('ALTER TABLE tasks ADD COLUMN migration_probe INTEGER DEFAULT 0')
        con.execute("UPDATE tasks SET title='must roll back'")

    def version_three(con):
        if failure == 'exception':
            raise RuntimeError('injected migration failure')
        if failure == 'invalid_schema':
            con.execute('ALTER TABLE tasks DROP COLUMN verify_authors')
        else:
            con.execute('PRAGMA defer_foreign_keys=ON')
            con.execute("INSERT INTO authors(task_id,author_key) VALUES('missing','invalid')")

    monkeypatch.setattr(migrations, 'MIGRATIONS', original + (
        Migration(2, version_two), Migration(3, version_three)))
    with pytest.raises(DatabaseMigrationError, match='启动已停止') as error:
        Database(path)
    assert snapshot(path) == before
    saved, = backups(path)
    assert error.value.backup_path == saved
    assert snapshot(saved) == before
    original_bytes = saved.read_bytes()
    # Retrying a fixed migration creates a new backup; it never overwrites the old one.
    monkeypatch.setattr(migrations, 'MIGRATIONS', original + (Migration(2, version_two),))
    Database(path)
    assert snapshot(path)[0] == 2
    assert len(backups(path)) == 2
    assert saved.read_bytes() == original_bytes


def test_new_database_failure_rolls_back_created_tables(tmp_path, monkeypatch):
    path = tmp_path / 'failed_new.sqlite3'

    def fail(con):
        con.execute('CREATE TABLE failed_probe(id INTEGER)')
        raise RuntimeError('injected')

    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS + (Migration(2, fail),))
    with pytest.raises(DatabaseMigrationError):
        Database(path)
    with closing(sqlite3.connect(path)) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 0
        assert con.execute('SELECT name FROM sqlite_schema').fetchall() == []
    assert not (tmp_path / 'backups').exists()


def test_interrupted_migration_rolls_back_before_propagating_interrupt(tmp_path, monkeypatch):
    path = tmp_path / 'interrupted.sqlite3'
    legacy_database(path)
    before = snapshot(path)

    def interrupt(con):
        con.execute("UPDATE checkpoints SET cursor='0'")
        raise KeyboardInterrupt

    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS + (Migration(2, interrupt),))
    with pytest.raises(KeyboardInterrupt):
        Database(path)
    assert snapshot(path) == before
    assert snapshot(backups(path)[0]) == before


def test_migration_failure_prevents_service_manager_startup(tmp_path, monkeypatch):
    from backend.app import create_app

    path = tmp_path / 'comments.sqlite3'
    legacy_database(path)
    calls = []

    def fail(con):
        raise RuntimeError('injected')

    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS + (Migration(2, fail),))
    with pytest.raises(DatabaseMigrationError):
        create_app(tmp_path, manager_factory=lambda *args: calls.append(args))
    assert calls == []
    assert snapshot(path)[0] == 0


def test_backup_directory_failure_does_not_start_migration(tmp_path):
    path = tmp_path / 'blocked.sqlite3'
    legacy_database(path)
    before = snapshot(path)
    (tmp_path / 'backups').write_text('existing user file', encoding='utf-8')
    with pytest.raises(DatabaseMigrationError) as error:
        Database(path)
    assert error.value.backup_path is None
    assert snapshot(path) == before
    assert (tmp_path / 'backups').read_text(encoding='utf-8') == 'existing user file'


def test_failed_backup_is_not_published_as_a_recoverable_backup(tmp_path, monkeypatch):
    path = tmp_path / 'backup_failure.sqlite3'
    legacy_database(path)
    before = snapshot(path)
    connect = sqlite3.connect

    class BrokenBackup(sqlite3.Connection):
        def backup(self, destination, **kwargs):
            destination.execute('CREATE TABLE incomplete(id INTEGER)')
            raise OSError('injected storage failure')

    def connect_with_failure(*args, **kwargs):
        if kwargs.get('uri'):
            kwargs['factory'] = BrokenBackup
        return connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(migrations.sqlite3, 'connect', connect_with_failure)
        with pytest.raises(DatabaseMigrationError):
            Database(path)
    assert snapshot(path) == before
    assert backups(path) == []
    assert len(list((tmp_path / 'backups').glob('*/backup.incomplete'))) == 1


def test_backup_can_restore_into_a_separate_database_and_upgrade_again(tmp_path):
    path = tmp_path / 'source.sqlite3'
    legacy_database(path)
    before = snapshot(path)
    Database(path)
    saved, = backups(path)
    restored = tmp_path / 'restored' / 'restored.sqlite3'
    restored.parent.mkdir()
    with closing(sqlite3.connect(saved.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(restored)) as destination:
            source.backup(destination)
    assert snapshot(restored) == before
    db = Database(restored)
    assert snapshot(restored) == (1, before[1])
    assert len(db.read_comments('comments')) == 2
    assert db.get_checkpoint('comments')['cursor'] == 40
    assert snapshot(saved) == before


def test_backup_and_migration_share_a_writer_exclusion_boundary(tmp_path, monkeypatch):
    path = tmp_path / 'locked.sqlite3'
    legacy_database(path)
    before = snapshot(path)
    create_backup = migrations._create_backup

    def probe(*args):
        with closing(sqlite3.connect(path, timeout=0)) as writer:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                writer.execute("UPDATE tasks SET title='competing write'")
        return create_backup(*args)

    monkeypatch.setattr(migrations, '_create_backup', probe)
    Database(path)
    assert snapshot(backups(path)[0]) == before


def test_two_processes_only_adopt_and_back_up_once(tmp_path):
    path = tmp_path / 'shared.sqlite3'
    legacy_database(path)
    command = [sys.executable, '-c',
        'from backend.db import Database; import sys; Database(sys.argv[1])', str(path)]

    def start():
        return subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, text=True, timeout=30)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: start(), range(2)))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    assert snapshot(path)[0] == 1
    assert len(backups(path)) == 1


@pytest.mark.parametrize('damage', ['future_version', 'missing_column', 'missing_index', 'missing_session'])
def test_invalid_versioned_database_fails_without_silent_repair(tmp_path, damage):
    path = tmp_path / 'invalid.sqlite3'
    Database(path)
    with closing(sqlite3.connect(path)) as con:
        if damage == 'future_version':
            con.execute('PRAGMA user_version=999')
        elif damage == 'missing_column':
            con.execute('ALTER TABLE tasks DROP COLUMN verify_authors')
        elif damage == 'missing_index':
            con.execute('DROP INDEX comments_time')
        else:
            con.execute('DELETE FROM session')
        con.commit()
    before = snapshot(path)
    with pytest.raises(DatabaseMigrationError):
        Database(path)
    assert snapshot(path) == before
    assert backups(path) == []


def test_unknown_database_is_not_adopted(tmp_path):
    path = tmp_path / 'other.sqlite3'
    with closing(sqlite3.connect(path)) as con:
        con.execute('CREATE TABLE unrelated(id INTEGER)')
    before = snapshot(path)
    with pytest.raises(DatabaseMigrationError, match='无法识别'):
        Database(path)
    assert snapshot(path) == before
    assert backups(path) == []


def test_migration_registry_requires_contiguous_versions(tmp_path, monkeypatch):
    path = tmp_path / 'registry.sqlite3'
    monkeypatch.setattr(migrations, 'MIGRATIONS', (Migration(2, lambda con: None),))
    with pytest.raises(DatabaseMigrationError, match='连续排列'):
        Database(path)
    assert not path.exists()
