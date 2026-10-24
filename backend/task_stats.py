"""Transactional task counters and result generations, installed by migration 5."""

INDEXES = {
    'likes_sequence': ('liked_videos', 'task_id,collected_order,created_at DESC,video_id DESC'),
    'verification_author': ('author_verifications', 'task_id,author_key'),
    'tasks_created': ('tasks', 'created_at DESC'),
}
COUNTERS = ('main_count', 'reply_count', 'video_count', 'missing_times', 'unknown_authors')
GROUP_KEYS = ('task_id', 'author_key', 'source_uid', 'source_sec_uid')


def active(ref):
    return f'''(EXISTS(SELECT 1 FROM comments c WHERE c.task_id={ref}.task_id AND c.comment_id={ref}.record_id)
        OR EXISTS(SELECT 1 FROM liked_videos l WHERE l.task_id={ref}.task_id AND l.video_id={ref}.record_id))'''


def bump(ref, condition='1'):
    return f'UPDATE task_stats SET result_version=result_version+1 WHERE task_id={ref}.task_id AND {condition};'


def group_change(query, add):
    keys = ','.join(GROUP_KEYS)
    if add:
        return f'''INSERT INTO verification_groups({keys},pending_records)
            SELECT {keys},1 FROM ({query}) WHERE 1
            ON CONFLICT({keys}) DO UPDATE SET pending_records=pending_records+1;'''
    return f'''UPDATE verification_groups SET pending_records=pending_records-1
        WHERE ({keys}) IN ({query});
        DELETE FROM verification_groups WHERE pending_records=0 AND ({keys}) IN ({query});'''


def verification_group(ref):
    return 'SELECT ' + ','.join(f'{ref}.{key} AS {key}' for key in GROUP_KEYS) + f" WHERE {ref}.status!='confirmed' AND {active(ref)}"


def reference_group(ref, table, id_column):
    other, other_id = ('liked_videos', 'video_id') if table == 'comments' else ('comments', 'comment_id')
    return f'''SELECT {','.join('v.'+key for key in GROUP_KEYS)} FROM author_verifications v
        WHERE v.task_id={ref}.task_id AND v.record_id={ref}.{id_column} AND v.status!='confirmed'
        AND NOT EXISTS(SELECT 1 FROM {other} other WHERE other.task_id={ref}.task_id AND other.{other_id}={ref}.{id_column})'''


def changed(fields):
    return ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)


def trigger_sql():
    triggers = {}

    def add(name, event, body, when=None):
        triggers[name] = f'CREATE TRIGGER {name} AFTER {event}' + (f' WHEN {when}' if when else '') + f' BEGIN {body} END'

    add('stats_task_insert', 'INSERT ON tasks', 'INSERT INTO task_stats(task_id) VALUES(NEW.id);')
    add('stats_task_result_update', 'UPDATE ON tasks',
        'UPDATE task_stats SET result_version=result_version+1 WHERE task_id=NEW.id;',
        changed(('collection_complete', 'visibility', 'list_order')))
    add('stats_group_insert', 'INSERT ON verification_groups',
        'UPDATE task_stats SET unknown_authors=unknown_authors+1 WHERE task_id=NEW.task_id;')
    add('stats_group_delete', 'DELETE ON verification_groups',
        'UPDATE task_stats SET unknown_authors=unknown_authors-1 WHERE task_id=OLD.task_id;')

    for table, id_column, fields in (
        ('comments', 'comment_id', 'task_id comment_id video_id parent_id text created_at ip_label nickname douyin_id uid sec_uid author_key identity_state reply_count text_source'),
        ('liked_videos', 'video_id', 'task_id video_id title author_name author_douyin_id created_at video_url author_key uid sec_uid action_at collected_order'),
    ):
        def counts(ref, sign):
            deltas = {'video_count': '1'} if table == 'liked_videos' else {
                'main_count': f"COALESCE({ref}.parent_id='',0)",
                'reply_count': f"COALESCE({ref}.parent_id!='',0)",
                'missing_times': f'({ref}.created_at IS NULL)',
            }
            return 'UPDATE task_stats SET ' + ','.join(f'{key}={key}{sign}{value}' for key, value in deltas.items()) + f' WHERE task_id={ref}.task_id;'
        add(f'stats_{table}_insert', f'INSERT ON {table}',
            counts('NEW', '+') + bump('NEW') + group_change(reference_group('NEW', table, id_column), True))
        add(f'stats_{table}_delete', f'DELETE ON {table}',
            counts('OLD', '-') + bump('OLD') + group_change(reference_group('OLD', table, id_column), False))
        add(f'stats_{table}_update', f'UPDATE ON {table}',
            counts('OLD', '-') + counts('NEW', '+') + bump('OLD') + bump('NEW', 'OLD.task_id IS NOT NEW.task_id'),
            changed(fields.split()))
        add(f'stats_{table}_rekey', f'UPDATE ON {table}',
            group_change(reference_group('OLD', table, id_column), False) +
            group_change(reference_group('NEW', table, id_column), True), changed(('task_id', id_column)))

    add('stats_verification_insert', 'INSERT ON author_verifications',
        group_change(verification_group('NEW'), True) + bump('NEW', active('NEW')))
    add('stats_verification_delete', 'DELETE ON author_verifications',
        group_change(verification_group('OLD'), False) + bump('OLD', active('OLD')))
    add('stats_verification_group_update', 'UPDATE ON author_verifications',
        group_change(verification_group('OLD'), False) + group_change(verification_group('NEW'), True),
        changed((*GROUP_KEYS, 'record_id')) + " OR (OLD.status='confirmed')!=(NEW.status='confirmed')")
    add('stats_verification_result_update', 'UPDATE ON author_verifications',
        bump('OLD', f'({active("OLD")} OR (OLD.task_id=NEW.task_id AND {active("NEW")}))') +
        bump('NEW', f'OLD.task_id IS NOT NEW.task_id AND {active("NEW")}'),
        changed((*GROUP_KEYS, 'record_id', 'source_number', 'source_conflict', 'uid', 'sec_uid', 'douyin_id', 'nickname', 'status', 'failure_kind')))
    for event, ref in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
        add(f'stats_author_{event.lower()}', f'{event} ON authors',
            bump(ref, f'EXISTS(SELECT 1 FROM comments c WHERE c.task_id={ref}.task_id AND c.author_key={ref}.author_key)'))
    old_link = 'EXISTS(SELECT 1 FROM comments c WHERE c.task_id=OLD.task_id AND c.author_key=OLD.author_key)'
    new_link = 'EXISTS(SELECT 1 FROM comments c WHERE c.task_id=NEW.task_id AND c.author_key=NEW.author_key)'
    add('stats_author_update', 'UPDATE ON authors',
        bump('OLD', f'({old_link} OR (OLD.task_id=NEW.task_id AND {new_link}))') +
        bump('NEW', f'OLD.task_id IS NOT NEW.task_id AND {new_link}'),
        changed(('task_id', 'author_key', 'uid', 'sec_uid', 'douyin_id', 'nickname', 'identity_state')))
    return triggers


def expected(con, task_id):
    main, replies, missing = con.execute('''SELECT COALESCE(SUM(parent_id=''),0),
        COALESCE(SUM(parent_id!=''),0),COALESCE(SUM(created_at IS NULL),0) FROM comments WHERE task_id=?''', (task_id,)).fetchone()
    videos = con.execute('SELECT COUNT(*) FROM liked_videos WHERE task_id=?', (task_id,)).fetchone()[0]
    groups = [tuple(row) for row in con.execute(f'''SELECT author_key,source_uid,source_sec_uid,COUNT(*)
        FROM author_verifications v WHERE task_id=? AND status!='confirmed' AND {active('v')}
        GROUP BY author_key,source_uid,source_sec_uid ORDER BY author_key,source_uid,source_sec_uid''', (task_id,))]
    return (main, replies, videos, missing, len(groups)), groups


def inspect(con, task_id=None):
    ids = [row[0] for row in con.execute('SELECT id FROM tasks' + (' WHERE id=?' if task_id else ''), (task_id,) if task_id else ())]
    differences = []
    for tid in ids:
        counts, groups = expected(con, tid)
        saved = con.execute('SELECT ' + ','.join(COUNTERS) + ' FROM task_stats WHERE task_id=?', (tid,)).fetchone()
        saved_groups = [tuple(row) for row in con.execute('''SELECT author_key,source_uid,source_sec_uid,pending_records
            FROM verification_groups WHERE task_id=? ORDER BY author_key,source_uid,source_sec_uid''', (tid,))]
        if saved is None or tuple(saved) != counts or saved_groups != groups:
            differences.append(tid)
    return differences


def rebuild(con, task_id=None, *, bump_version=True):
    ids = [row[0] for row in con.execute('SELECT id FROM tasks' + (' WHERE id=?' if task_id else ''), (task_id,) if task_id else ())]
    for tid in ids:
        counts, groups = expected(con, tid)
        # Group triggers may update the old count while replacing membership;
        # set the final independently computed counters in this transaction.
        con.execute('DELETE FROM verification_groups WHERE task_id=?', (tid,))
        con.execute('INSERT OR IGNORE INTO task_stats(task_id) VALUES(?)', (tid,))
        con.executemany('''INSERT INTO verification_groups(task_id,author_key,source_uid,source_sec_uid,pending_records)
            VALUES(?,?,?,?,?)''', ((tid, *group) for group in groups))
        con.execute('UPDATE task_stats SET ' + ','.join(f'{key}=?' for key in COUNTERS) +
            ',result_version=result_version+? WHERE task_id=?', (*counts, int(bump_version), tid))
    return len(ids)


def migrate(con):
    con.execute('''CREATE TABLE task_stats (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        main_count INTEGER NOT NULL DEFAULT 0,reply_count INTEGER NOT NULL DEFAULT 0,
        video_count INTEGER NOT NULL DEFAULT 0,missing_times INTEGER NOT NULL DEFAULT 0,
        unknown_authors INTEGER NOT NULL DEFAULT 0,result_version INTEGER NOT NULL DEFAULT 0)''')
    con.execute('''CREATE TABLE verification_groups (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        author_key TEXT NOT NULL,source_uid TEXT NOT NULL,source_sec_uid TEXT NOT NULL,
        pending_records INTEGER NOT NULL,
        PRIMARY KEY(task_id,author_key,source_uid,source_sec_uid))''')
    for name, (table, columns) in INDEXES.items():
        con.execute(f'CREATE INDEX {name} ON {table}({columns})')
    rebuild(con, bump_version=False)
    for sql in trigger_sql().values():
        con.execute(sql)
