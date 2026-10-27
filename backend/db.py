"""SQLite persistence shared by the API and the isolated collector process."""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .migrations import initialize_database
from .task_state import transition_fields


class TaskNotFound(KeyError):
    pass


class TaskSnapshot:
    """Task metadata and result reads pinned to one SQLite transaction."""
    def __init__(self, db, con, task_id):
        self.db, self.con, self.task_id = db, con, task_id
        self.task = db._get_task(con, task_id)

    def comments(self, filters, *, limit=None, offset=0):
        return self.db._read_comment_page(self.con, self.task_id, filters, limit=limit, offset=offset)

    def videos(self, *, limit=None, offset=0):
        rows = self.db._read_liked_videos(self.con, self.task_id, limit=limit, offset=offset)
        return rows, self.task['video_count']

    def comment_batches(self, filters, check_cancel):
        return self.db._iter_comment_batches(self.con, self.task_id, filters, check_cancel)

    def video_batches(self, check_cancel):
        return self.db._iter_video_batches(self.con, self.task_id, check_cancel)


class Database:
    def __init__(self, path, *, keyword_cache=None):
        self.path = str(Path(path).resolve())
        self.keyword_cache = keyword_cache
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        initialize_database(Path(self.path))

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            with con:
                yield con
        finally:
            con.close()

    def create_task(self, source_url, video_id='', *, kind='comments', target_account='', max_videos=0,
                    start_time=None, end_time=None, list_order='publication', verify_authors=False):
        if type(verify_authors) is not bool:
            raise ValueError('Invalid author verification option')
        if kind not in ('comments', 'likes', 'recommendations'):
            raise ValueError('Unknown task kind')
        if type(max_videos) is not int or max_videos < 0:
            raise ValueError('Invalid video limit')
        if list_order not in ('publication', 'activity'):
            raise ValueError('Invalid list order')
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError('Invalid time range')
        task_id = uuid.uuid4().hex
        now = time.time()
        with self.connection() as con:
            con.execute('INSERT INTO tasks(id,source_url,video_id,created_at,updated_at,kind,target_account,max_videos,start_time,end_time,list_order,verify_authors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (task_id, source_url, str(video_id), now, now, kind, target_account, max_videos, start_time, end_time, list_order, verify_authors))
        return self.get_task(task_id)

    def get_task(self, task_id):
        with self.connection() as con:
            con.execute('BEGIN')
            return self._get_task(con, task_id)

    @contextmanager
    def task_snapshot(self, task_id):
        with self.connection() as con:
            con.execute('BEGIN')
            yield TaskSnapshot(self, con, task_id)

    TASK_SELECT = '''SELECT t.*,s.main_count,s.reply_count,s.video_count,s.unknown_authors,s.result_version
        FROM tasks t JOIN task_stats s ON s.task_id=t.id'''

    @staticmethod
    def _format_task(row):
        result = dict(row)
        result['collection_complete'] = bool(result['collection_complete'])
        result['verify_authors'] = bool(result['verify_authors'])
        result['verification_complete'] = result['unknown_authors'] == 0
        result.pop('stop_requested', None)
        return result

    @classmethod
    def _get_task(cls, con, task_id):
        row = con.execute(cls.TASK_SELECT + ' WHERE t.id=?', (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return cls._format_task(row)

    def list_tasks(self):
        with self.connection() as con:
            return [self._format_task(row) for row in con.execute(self.TASK_SELECT + ' ORDER BY t.created_at DESC')]

    def check_statistics(self, task_id=None):
        from .task_stats import inspect
        with self.connection() as con:
            con.execute('BEGIN')
            return inspect(con, task_id)

    def rebuild_statistics(self, task_id=None):
        from .task_stats import rebuild
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            return rebuild(con, task_id)

    def update_task(self, task_id, **fields):
        with self.connection() as con:
            self._update_task(con, task_id, fields)

    @staticmethod
    def _update_task(con, task_id, fields):
        allowed = {'source_url', 'video_id', 'title', 'status', 'error', 'stop_requested', 'collection_complete', 'verification_complete',
                   'target_sec_uid', 'target_nickname', 'visibility', 'verify_authors'}
        if 'verify_authors' in fields and type(fields['verify_authors']) is not bool:
            raise ValueError('Invalid author verification option')
        if not fields.keys() <= allowed:
            raise ValueError('Unknown task fields: ' + ','.join(fields.keys() - allowed))
        fields['updated_at'] = time.time()
        con.execute('UPDATE tasks SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', (*fields.values(), task_id))

    def transition_task(self, task_id, event, **options):
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            if event == 'finish':
                task = self._get_task(con, task_id)
            else:
                row = con.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                if row is None:
                    raise TaskNotFound(task_id)
                task = dict(row)
            fields = transition_fields(task, event, **options)
            if fields:
                self._update_task(con, task_id, fields)

    def should_stop(self, task_id):
        with self.connection() as con:
            row = con.execute('SELECT stop_requested FROM tasks WHERE id=?', (task_id,)).fetchone()
            return row is None or bool(row[0])

    def delete_task(self, task_id):
        with self.connection() as con:
            con.execute('DELETE FROM tasks WHERE id=?', (task_id,))
        if self.keyword_cache is not None:
            self.keyword_cache.discard(task_id)

    def recover_interrupted(self):
        with self.connection() as con:
            con.execute("UPDATE tasks SET status='paused',error='上次运行已中断，可继续采集',stop_requested=1 WHERE status IN ('running','verifying','waiting_login')")
            con.execute("UPDATE session SET status='logged_out',error=NULL WHERE status='waiting'")

    def get_checkpoint(self, task_id, thread_id=''):
        with self.connection() as con:
            row = con.execute('SELECT cursor,done,restarts FROM checkpoints WHERE task_id=? AND thread_id=?', (task_id, str(thread_id))).fetchone()
            if not row:
                return {'cursor': 0, 'done': False, 'restarts': 0}
            return {'cursor': int(row['cursor']), 'done': bool(row['done']), 'restarts': row['restarts']}

    def reset_checkpoint(self, task_id, thread_id=''):
        with self.connection() as con:
            con.execute("INSERT INTO checkpoints(task_id,thread_id,cursor,done,restarts) VALUES(?,?,'0',0,1) ON CONFLICT(task_id,thread_id) DO UPDATE SET cursor='0',done=0,restarts=restarts+1", (task_id, str(thread_id)))

    def save_page(self, task_id, thread_id, comments, next_cursor, done):
        thread_id = str(thread_id)
        cols = ('comment_id','video_id','parent_id','text','created_at','ip_label','nickname','douyin_id','uid','sec_uid','author_key','identity_state','reply_count','text_source')
        with self.connection() as con:
            # Read/merge/write and the cursor advance must observe one state.
            con.execute('BEGIN IMMEDIATE')
            for item in comments:
                c = self._merge_comment(con, task_id, item)
                parent_id = str(c.get('parent_id') or '')
                if not parent_id and (c.get('reply_count') or 0) > 0:
                    self._observe_replies(con, task_id, c['comment_id'], c['reply_count'])
                elif parent_id and parent_id != thread_id:
                    exists = con.execute('SELECT 1 FROM comments WHERE task_id=? AND comment_id=?',
                                         (task_id, c['comment_id'])).fetchone()
                    if not exists:
                        self._observe_replies(con, task_id, parent_id, new_preview=True)
                values = [c.get(k, 0 if k == 'reply_count' else None if k == 'created_at' else 'unknown' if k == 'identity_state' else '') for k in cols]
                con.execute('INSERT INTO comments(task_id,' + ','.join(cols) + ') VALUES(' + ','.join('?' for _ in range(len(cols)+1)) + ') ON CONFLICT(task_id,comment_id) DO UPDATE SET ' + ','.join(f'{k}=excluded.{k}' for k in cols if k != 'comment_id'), (task_id, *values))
                from .verification import sync_record
                sync_record(con, task_id, c['comment_id'], c['author_key'], c['uid'], c['sec_uid'],
                            c['douyin_id'], c['nickname'], c['identity_state'] == 'conflict')
            con.execute('INSERT INTO checkpoints(task_id,thread_id,cursor,done) VALUES(?,?,?,?) ON CONFLICT(task_id,thread_id) DO UPDATE SET cursor=excluded.cursor,done=excluded.done', (task_id, str(thread_id), str(next_cursor), bool(done)))
            if thread_id:
                con.execute('''UPDATE checkpoints SET reply_count_seen=MAX(COALESCE(reply_count_seen,0),
                    COALESCE((SELECT reply_count FROM comments WHERE task_id=? AND comment_id=? AND parent_id=''),0)),
                    reply_rows_seen=CASE WHEN ? THEN (SELECT COUNT(*) FROM comments WHERE task_id=? AND parent_id=?)
                        ELSE reply_rows_seen END WHERE task_id=? AND thread_id=?''',
                    (task_id, thread_id, bool(done), task_id, thread_id, task_id, thread_id))
            con.execute('UPDATE tasks SET updated_at=? WHERE id=?', (time.time(),task_id))

    def _merge_comment(self, con, task_id, item):
        old = con.execute('SELECT * FROM comments WHERE task_id=? AND comment_id=?',
                          (task_id, item['comment_id'])).fetchone()
        present = item.get('_present_fields', item.keys())
        preview = item.get('_source') == 'preview'
        c = dict(old) if old else {k: item.get(k, '') for k in ('comment_id', 'video_id', 'parent_id')}
        if not old:
            c.update(text='', created_at=None, ip_label='', nickname='', uid='', sec_uid='',
                     douyin_id='', identity_state='unknown', reply_count=item.get('reply_count', 0), text_source='legacy')
        if 'text' in present and isinstance(item.get('text'), str):
            if not preview or (not c['text'] and c['text_source'] != 'full'):
                c['text'] = item['text']
                c['text_source'] = 'preview' if preview else 'full'
        for key in ('created_at', 'ip_label', 'nickname', 'reply_count'):
            if key in present and item.get(key) not in (None, ''):
                if not preview or c.get(key) in (None, '', 0):
                    c[key] = item[key]

        # The comment ID fixes the author association. Missing user objects must
        # not replace it; contradictory identifiers remain pending verification.
        previous = None
        if old:
            previous = con.execute('SELECT * FROM authors WHERE task_id=? AND author_key=?',
                                   (task_id, old['author_key'])).fetchone()
        if previous:
            c['identity_state'] = previous['identity_state']
        identities = ('uid', 'sec_uid', 'douyin_id')
        expected = {key: (previous[key] if previous else '') or c[key] for key in identities}
        conflict = any(item.get(key) and key in present and expected[key] and expected[key] != item[key]
                       for key in identities)
        if conflict:
            c['nickname'] = old['nickname'] or (previous['nickname'] if previous else '')
        for key in identities:
            if not conflict and key in present and item.get(key):
                c[key] = item[key]
        temporary = f"comment:{c['comment_id']}"
        if old and (old['author_key'] != temporary or conflict):
            c['author_key'] = old['author_key']
        else:
            c['author_key'] = (item.get('author_key') if not old else '') or c['uid'] or c['sec_uid'] or temporary
        if conflict or item.get('identity_state') == 'conflict' or c['identity_state'] == 'conflict' or (previous and previous['identity_state'] == 'conflict'):
            c['identity_state'] = 'conflict'
        elif c['douyin_id']:
            c['identity_state'] = 'verified'
        if old and previous and c['author_key'] == old['author_key']:
            # Do not feed stale raw comment fields back into a hydrated profile.
            fields = {key: item[key] for key in (*identities, 'nickname')
                      if key in present and item.get(key) and not conflict}
        else:
            # Carry known data across a temporary-key upgrade, without inferring
            # identities from nicknames or unrelated orphaned authors.
            fields = {key: c.get(key) or (previous[key] if previous else '')
                      for key in (*identities, 'nickname')}
        fields['identity_state'] = c['identity_state']
        author = self._save_author(con, task_id, c['author_key'],
            preserve_nickname=preview or 'nickname' not in present, **fields)
        c['identity_state'] = author['identity_state']
        return c

    @staticmethod
    def _observe_replies(con, task_id, thread_id, declared_count=0, *, new_preview=False):
        # A completed thread only reopens for new evidence, never because the
        # declared count differs from the number of replies the server supplied.
        changed = con.execute('''INSERT INTO checkpoints(task_id,thread_id,reply_count_seen)
            VALUES(?,?,?) ON CONFLICT(task_id,thread_id) DO UPDATE SET
                cursor=CASE WHEN done=1 THEN '0' ELSE cursor END,
                restarts=restarts+CASE WHEN done=1 THEN 1 ELSE 0 END, done=0,
                reply_count_seen=MAX(COALESCE(reply_count_seen,0),excluded.reply_count_seen)
            WHERE excluded.reply_count_seen>COALESCE(reply_count_seen,0) OR ?''',
            (task_id, str(thread_id), declared_count, new_preview)).rowcount
        if changed:
            con.execute('UPDATE tasks SET collection_complete=0 WHERE id=?', (task_id,))

    @staticmethod
    def _save_author(con, task_id, author_key, *, profile_verified=False, preserve_nickname=False, **fields):
        old = con.execute('SELECT * FROM authors WHERE task_id=? AND author_key=?', (task_id,author_key)).fetchone()
        merged = dict(old) if old else {k:'' for k in ('uid','sec_uid','douyin_id','nickname','identity_state')}
        conflict = bool(old and any(old[k] and fields.get(k) and old[k] != str(fields[k]) for k in ('uid','sec_uid','douyin_id')))
        needs_verification = not profile_verified and (conflict or merged['identity_state'] == 'conflict' or fields.get('identity_state') == 'conflict')
        for k,v in fields.items():
            if k in merged and v:
                if k == 'nickname' and (preserve_nickname or needs_verification) and merged[k]:
                    continue
                if needs_verification and k in ('uid','sec_uid','douyin_id') and merged[k]:
                    continue
                if k == 'identity_state' and merged[k] == 'verified' and v == 'unknown':
                    continue
                merged[k] = str(v)
        if needs_verification:
            merged['identity_state'] = 'conflict'
            if not old or old['identity_state'] != 'conflict':
                con.execute('''UPDATE author_verifications SET source_conflict=1,status='conflict',
                    failure_kind='identity_conflict' WHERE task_id=? AND author_key=?''', (task_id, author_key))
        con.execute('''INSERT INTO authors(task_id,author_key,uid,sec_uid,douyin_id,nickname,identity_state)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,author_key) DO UPDATE SET
            uid=excluded.uid,sec_uid=excluded.sec_uid,douyin_id=excluded.douyin_id,
            nickname=excluded.nickname,identity_state=excluded.identity_state''',
                    (task_id, author_key, *(merged[k] for k in ('uid','sec_uid','douyin_id','nickname','identity_state'))))
        return merged

    def save_author(self, task_id, author_key, **fields):
        with self.connection() as con:
            self._save_author(con, task_id, author_key, profile_verified=fields.get('identity_state') == 'verified', **fields)
            con.execute('UPDATE tasks SET updated_at=? WHERE id=?', (time.time(),task_id))
        self._save_legacy_author_result(task_id, author_key, fields)

    def authors(self, task_id, unresolved_only=True):
        with self.connection() as con:
            sql = '''SELECT * FROM authors a WHERE task_id=? AND EXISTS(
                SELECT 1 FROM comments c WHERE c.task_id=a.task_id AND c.author_key=a.author_key)'''
            if unresolved_only:
                sql += " AND (identity_state!='verified' OR douyin_id='')"
            return [dict(r) for r in con.execute(sql,(task_id,))]

    def list_threads(self, task_id):
        with self.connection() as con:
            return [dict(r) for r in con.execute("SELECT comment_id,reply_count FROM comments WHERE task_id=? AND parent_id='' AND reply_count>0", (task_id,))]

    def pending_threads(self, task_id):
        with self.connection() as con:
            return [dict(r) for r in con.execute('''SELECT c.comment_id,c.reply_count
                FROM comments c LEFT JOIN checkpoints p ON p.task_id=c.task_id AND p.thread_id=c.comment_id
                WHERE c.task_id=? AND c.parent_id='' AND (c.reply_count>0 OR p.thread_id IS NOT NULL)
                    AND COALESCE(p.done,0)=0''', (task_id,))]

    def comments_complete(self, task_id):
        with self.connection() as con:
            return bool(con.execute('''SELECT EXISTS(SELECT 1 FROM checkpoints WHERE task_id=? AND thread_id='' AND done=1)
                AND NOT EXISTS(SELECT 1 FROM comments c LEFT JOIN checkpoints p
                    ON p.task_id=c.task_id AND p.thread_id=c.comment_id
                    WHERE c.task_id=? AND c.parent_id='' AND (c.reply_count>0 OR p.thread_id IS NOT NULL)
                        AND COALESCE(p.done,0)=0)''', (task_id, task_id)).fetchone()[0])

    COMMENT_SELECT = '''SELECT c.*,
        COALESCE(v.douyin_id,NULLIF(a.douyin_id,''),c.douyin_id) AS public_number,
        COALESCE(NULLIF(c.nickname,''),NULLIF(v.nickname,''),a.nickname,'') AS display_name,
        CASE WHEN v.status='confirmed' THEN 'verified' WHEN v.status='conflict' THEN 'conflict'
            WHEN v.status IS NOT NULL THEN 'unknown'
            ELSE COALESCE(a.identity_state,c.identity_state) END AS resolved_state
        FROM comments c LEFT JOIN authors a ON c.task_id=a.task_id AND c.author_key=a.author_key
        LEFT JOIN author_verifications v ON v.task_id=c.task_id AND v.record_id=c.comment_id'''

    def read_comment_page(self, task_id, filters, *, limit, offset=0):
        with self.connection() as con:
            con.execute('BEGIN')
            return self._read_comment_page(con, task_id, filters, limit=limit, offset=offset)

    def _comment_query(self, task_id, filters):
        params = [task_id]
        where = ' WHERE c.task_id=?'
        if filters.account:
            # Match author keys before time filtering, exactly like select_comments.
            where += ''' AND c.author_key IN (SELECT author_key FROM (''' + self.COMMENT_SELECT + ''' WHERE c.task_id=?)
                WHERE resolved_state='verified' AND public_number=?)'''
            params.extend((task_id, filters.account))
        for operator, bound in (('>=', filters.start_ts), ('<=', filters.end_ts)):
            if bound is not None:
                where += f' AND c.created_at{operator}?'
                params.append(bound)
        return self.COMMENT_SELECT + where, params

    def _read_comment_page(self, con, task_id, filters, *, limit, offset=0):
        from .search import format_comment, match_text
        from .search_cache import match_key
        if (limit is not None and (type(limit) is not int or limit < 0)) or type(offset) is not int or offset < 0:
            raise ValueError('Invalid comment page')
        query, params = self._comment_query(task_id, filters)
        order = ' ORDER BY c.created_at DESC,c.comment_id DESC'
        stats = con.execute('SELECT missing_times,result_version FROM task_stats WHERE task_id=?', (task_id,)).fetchone()
        missing = stats[0] if stats else 0
        if not filters.keyword:
            total = con.execute('SELECT COUNT(*) FROM (' + query + ')', params).fetchone()[0]
            rows = con.execute(query + order + ' LIMIT ? OFFSET ?', (*params, min(limit, 2**63-1) if limit is not None else -1, min(offset, 2**63-1)))
            items = [format_comment(dict(row)) for row in rows]
        else:
            cache = self.keyword_cache
            build = None
            if cache is not None and stats is not None:
                key = match_key(task_id, stats['result_version'], filters)
                matches = cache.get(key)
                if matches is not None:
                    items = self._matched_comment_page(con, task_id, matches, limit, offset)
                    return items, len(matches), missing
                build = cache.begin(key)
            total, items = 0, []
            cursor = None
            try:
                cursor = con.execute(query + order, params)
                while batch := cursor.fetchmany(256):
                    for row in batch:
                        matched, kind, score = match_text(row['text'], filters)
                        if matched:
                            if offset <= total and (limit is None or total < offset + limit):
                                items.append(format_comment(dict(row), kind, score))
                            total += 1
                            if cache is not None:
                                cache.append(build, row['comment_id'], kind, score)
                if cache is not None:
                    cache.finish(build, lambda: self._current_result_version(task_id))
            finally:
                if cursor is not None:
                    cursor.close()
                if cache is not None:
                    cache.abort(build)
        return items, total, missing

    def _current_result_version(self, task_id):
        try:
            with self.connection() as con:
                row = con.execute('SELECT result_version FROM task_stats WHERE task_id=?', (task_id,)).fetchone()
                return row[0] if row else None
        except sqlite3.Error:
            # Failure to admit a cache entry must not fail a valid result read.
            return None

    def _matched_comment_page(self, con, task_id, matches, limit, offset):
        from .search import format_comment
        end = len(matches) if limit is None else min(len(matches), offset + limit)
        items = []
        for start in range(offset, end, 256):
            batch = matches[start:min(start + 256, end)]
            ids = [item[0] for item in batch]
            rows = con.execute(self.COMMENT_SELECT +
                ' WHERE c.task_id=? AND c.comment_id IN (' + ','.join('?' for _ in ids) + ')', (task_id, *ids))
            by_id = {row['comment_id']: dict(row) for row in rows}
            items.extend(format_comment(by_id[cid], kind, score) for cid, kind, score in batch)
        return items

    def _iter_comment_batches(self, con, task_id, filters, check_cancel):
        from .search import format_comment, match_text
        from .search_cache import match_key
        check_cancel()
        if filters.keyword and self.keyword_cache is not None:
            stats = con.execute('SELECT result_version FROM task_stats WHERE task_id=?', (task_id,)).fetchone()
            matches = self.keyword_cache.get(match_key(task_id, stats[0], filters)) if stats else None
            if matches is not None:
                for offset in range(0, len(matches), 256):
                    check_cancel()
                    yield self._matched_comment_page(con, task_id, matches, 256, offset)
                return
        # A cold export streams its matches instead of retaining another full ID list.
        query, params = self._comment_query(task_id, filters)
        cursor = con.execute(query + ' ORDER BY c.created_at DESC,c.comment_id DESC', params)
        try:
            while rows := cursor.fetchmany(256):
                items = []
                for row in rows:
                    check_cancel()
                    matched, kind, score = match_text(row['text'], filters)
                    if matched:
                        items.append(format_comment(dict(row), kind, score))
                if items:
                    yield items
        finally:
            cursor.close()

    def read_comments(self, task_id):
        # A single SQL read yields a consistent export/search snapshot, including hydrated identity.
        with self.connection() as con:
            return [dict(r) for r in con.execute(self.COMMENT_SELECT +
                ' WHERE c.task_id=? ORDER BY c.created_at DESC,c.comment_id DESC', (task_id,))]

    def session_get(self):
        with self.connection() as con:
            return dict(con.execute('SELECT status,error FROM session WHERE id=1').fetchone())

    def session_set(self, status, error=None):
        with self.connection() as con:
            con.execute('UPDATE session SET status=?,error=? WHERE id=1',(status,error))

    def save_likes_page(self, task_id, videos, next_cursor, done, *, checkpoint_key='likes'):
        cols = ('video_id', 'title', 'author_name', 'author_douyin_id', 'created_at',
                'video_url', 'author_key', 'uid', 'sec_uid', 'action_at')
        updates = []
        for key in cols[1:]:
            if key == 'action_at':
                updates.append('action_at=COALESCE(excluded.action_at,liked_videos.action_at)')
            elif key in ('author_name', 'author_douyin_id', 'uid', 'sec_uid'):
                updates.append(f"{key}=CASE WHEN liked_videos.author_key=excluded.author_key THEN COALESCE(NULLIF(excluded.{key},''),liked_videos.{key}) ELSE excluded.{key} END")
            else:
                updates.append(f'{key}=excluded.{key}')
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            next_order = con.execute('SELECT COALESCE(MAX(collected_order),0) FROM liked_videos WHERE task_id=?', (task_id,)).fetchone()[0] + 1
            affected = {}
            for video in videos:
                values = [video.get(k, None if k in ('created_at', 'action_at') else '') for k in cols]
                con.execute('INSERT INTO liked_videos(task_id,' + ','.join(cols) + ') VALUES(' + ','.join('?' for _ in range(len(cols)+1)) + ') ON CONFLICT(task_id,video_id) DO UPDATE SET ' + ','.join(updates), (task_id, *values))
                con.execute('UPDATE liked_videos SET collected_order=? WHERE task_id=? AND video_id=? AND collected_order IS NULL', (next_order, task_id, video['video_id']))
                next_order += 1
                affected.setdefault(video['author_key'], set()).add(video['video_id'])
            from .verification import sync_record
            for author_key, ids in affected.items():
                counts = con.execute('''SELECT COUNT(DISTINCT NULLIF(uid,'')),COUNT(DISTINCT NULLIF(sec_uid,''))
                    FROM liked_videos WHERE task_id=? AND author_key=?''', (task_id, author_key)).fetchone()
                conflict = any(count > 1 for count in counts)
                if conflict:
                    con.execute('''UPDATE author_verifications SET source_conflict=1,status='conflict',failure_kind='identity_conflict'
                        WHERE task_id=? AND author_key=? AND source_conflict=0''', (task_id, author_key))
                for video_id in ids:
                    row = con.execute('SELECT * FROM liked_videos WHERE task_id=? AND video_id=?', (task_id, video_id)).fetchone()
                    sync_record(con, task_id, row['video_id'], row['author_key'], row['uid'], row['sec_uid'],
                                row['author_douyin_id'], row['author_name'], conflict)
            con.execute("INSERT INTO checkpoints(task_id,thread_id,cursor,done) VALUES(?,?,?,?) ON CONFLICT(task_id,thread_id) DO UPDATE SET cursor=excluded.cursor,done=excluded.done", (task_id, checkpoint_key, str(next_cursor), bool(done)))
            con.execute('UPDATE tasks SET updated_at=? WHERE id=?', (time.time(), task_id))

    def liked_video_ids(self, task_id):
        with self.connection() as con:
            return {row[0] for row in con.execute('SELECT video_id FROM liked_videos WHERE task_id=?', (task_id,))}

    def read_liked_videos(self, task_id, *, limit=None, offset=0):
        with self.connection() as con:
            return self._read_liked_videos(con, task_id, limit=limit, offset=offset)

    def read_liked_video_page(self, task_id, *, limit, offset=0):
        with self.connection() as con:
            # Python's sqlite3 does not begin a transaction for SELECT. An
            # explicit read transaction keeps the count and rows consistent
            # even when the collector commits another page between these reads.
            con.execute('BEGIN')
            stats = con.execute('SELECT video_count FROM task_stats WHERE task_id=?', (task_id,)).fetchone()
            total = stats[0] if stats else 0
            rows = self._read_liked_videos(con, task_id, limit=limit, offset=offset)
        return rows, total

    @staticmethod
    def _video_query(con, task_id):
        task = con.execute('SELECT list_order FROM tasks WHERE id=?', (task_id,)).fetchone()
        order = 'collected_order ASC NULLS LAST,created_at DESC,video_id DESC' if task and task['list_order'] == 'activity' else 'created_at DESC,video_id DESC'
        sql = '''SELECT liked_videos.*,v.status AS verification_state,v.douyin_id AS verified_number,
            v.nickname AS verified_name FROM liked_videos LEFT JOIN author_verifications v
            ON v.task_id=liked_videos.task_id AND v.record_id=liked_videos.video_id
            WHERE liked_videos.task_id=? ORDER BY ''' + order
        return sql

    @classmethod
    def _read_liked_videos(cls, con, task_id, *, limit=None, offset=0):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError('Invalid video page limit')
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid video page offset')
        sql, params = cls._video_query(con, task_id), (task_id,)
        if limit is not None or offset:
            sql += ' LIMIT ? OFFSET ?'
            # Arbitrarily large API page numbers should remain empty pages,
            # rather than overflowing SQLite's signed integer binding.
            params += (min(limit, 2**63 - 1) if limit is not None else -1, min(offset, 2**63 - 1))
        return [cls._format_video(dict(row)) for row in con.execute(sql, params)]

    @staticmethod
    def _format_video(row):
        from .search import time_text
        state, number, name = (row.pop(key) for key in ('verification_state', 'verified_number', 'verified_name'))
        if state is not None:
            row['author_douyin_id'] = number if state == 'confirmed' else ''
            row['author_name'] = row['author_name'] or name or ''
        row['time_text'] = time_text(row['created_at'])
        return row

    def _iter_video_batches(self, con, task_id, check_cancel):
        check_cancel()
        cursor = con.execute(self._video_query(con, task_id), (task_id,))
        try:
            while rows := cursor.fetchmany(256):
                check_cancel()
                yield [self._format_video(dict(row)) for row in rows]
        finally:
            cursor.close()

    def liked_authors(self, task_id):
        return [{**r, 'uid': r['source_uid'], 'sec_uid': r['source_sec_uid']}
                for r in self.verification_records(task_id) if r['status'] != 'confirmed']

    def cached_account_profiles(self, account):
        # Previously verified public profiles are discovery hints only. The
        # collector must fetch and reverify the profile before using a hint.
        with self.connection() as con:
            return [dict(r) for r in con.execute("""SELECT DISTINCT uid,sec_uid FROM authors
                WHERE identity_state='verified' AND sec_uid!='' AND douyin_id=? COLLATE NOCASE
                UNION SELECT DISTINCT uid,sec_uid FROM liked_videos
                WHERE sec_uid!='' AND author_douyin_id=? COLLATE NOCASE
                UNION SELECT DISTINCT uid,sec_uid FROM author_verifications v
                WHERE status='confirmed' AND sec_uid!='' AND douyin_id=? COLLATE NOCASE AND
                (EXISTS(SELECT 1 FROM comments c WHERE c.task_id=v.task_id AND c.comment_id=v.record_id)
                 OR EXISTS(SELECT 1 FROM liked_videos l WHERE l.task_id=v.task_id AND l.video_id=v.record_id))""",
                (account, account, account))]

    def save_liked_author(self, task_id, author_key, author_name='', author_douyin_id=''):
        self._save_legacy_author_result(task_id, author_key, {'nickname': author_name, 'douyin_id': author_douyin_id})

    def _save_legacy_author_result(self, task_id, author_key, fields):
        # Compatibility for local callers: no representative ID may be invented
        # for a group containing different identities.
        from .verification import profile_result
        records = [r for r in self.verification_records(task_id) if r['author_key'] == author_key]
        if not records:
            return
        identities = {(r['source_uid'], r['source_sec_uid']) for r in records}
        if not fields.get('uid') and not fields.get('sec_uid'):
            if len(identities) != 1:
                raise ValueError('Author group does not have one unambiguous identity')
            uid, sec_uid = next(iter(identities))
            fields = {**fields, 'uid': uid, 'sec_uid': sec_uid}
        response = {'user': {**fields, 'unique_id': fields.get('douyin_id', '')}}
        for record in records:
            expected = {**record, 'uid': record['source_uid'], 'sec_uid': record['source_sec_uid']}
            status, reason, verified = profile_result(response, expected)
            self.finish_verification(task_id, record, status, reason, verified)

    def verification_records(self, task_id):
        with self.connection() as con:
            return [dict(r) for r in con.execute('''SELECT * FROM author_verifications v WHERE task_id=? AND
                (EXISTS(SELECT 1 FROM comments c WHERE c.task_id=v.task_id AND c.comment_id=v.record_id)
                 OR EXISTS(SELECT 1 FROM liked_videos l WHERE l.task_id=v.task_id AND l.video_id=v.record_id))
                ORDER BY record_id''', (task_id,))]

    @staticmethod
    def _verification_where(task_id, record):
        return (task_id, record['record_id'], record['author_key'], record['source_uid'], record['source_sec_uid'],
                record['source_number'], record['source_conflict'])

    VERIFICATION_WHERE = ''' WHERE task_id=? AND record_id=? AND author_key=? AND source_uid=?
        AND source_sec_uid=? AND source_number=? AND source_conflict=?'''

    def begin_verification(self, task_id, record):
        self.begin_verifications(task_id, [record])

    def begin_verifications(self, task_id, records):
        now = time.time()
        with self.connection() as con:
            con.executemany('UPDATE author_verifications SET attempts=attempts+1,last_attempt_at=?' + self.VERIFICATION_WHERE,
                            ((now, *self._verification_where(task_id, record)) for record in records))

    def finish_verification(self, task_id, record, status, failure_kind='', fields=None):
        self.finish_verifications(task_id, [(record, status, failure_kind, fields)])

    def finish_verifications(self, task_id, results):
        with self.connection() as con:
            changed = 0
            for record, status, failure_kind, fields in results:
                updates = {'status': status, 'failure_kind': failure_kind}
                if fields:
                    updates.update({key: fields[key] for key in ('uid', 'sec_uid', 'douyin_id', 'nickname')})
                changed += con.execute('UPDATE author_verifications SET ' + ','.join(f'{key}=?' for key in updates)
                    + self.VERIFICATION_WHERE, (*updates.values(), *self._verification_where(task_id, record))).rowcount
            if changed:
                con.execute('UPDATE tasks SET updated_at=? WHERE id=?', (time.time(), task_id))
