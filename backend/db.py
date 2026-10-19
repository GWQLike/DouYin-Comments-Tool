"""SQLite persistence shared by the API and the isolated collector process."""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .migrations import initialize_database


class Database:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
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
            row = con.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            result = dict(row)
            counts = con.execute("SELECT SUM(parent_id='') AS main_count,SUM(parent_id!='') AS reply_count FROM comments WHERE task_id=?", (task_id,)).fetchone()
            result.update({k: counts[k] or 0 for k in ('main_count', 'reply_count')})
            result['unknown_authors'] = con.execute('''SELECT COUNT(*) FROM authors a WHERE task_id=?
                AND (identity_state!='verified' OR douyin_id='')
                AND EXISTS(SELECT 1 FROM comments c WHERE c.task_id=a.task_id AND c.author_key=a.author_key)''', (task_id,)).fetchone()[0]
            result['video_count'] = con.execute('SELECT COUNT(*) FROM liked_videos WHERE task_id=?', (task_id,)).fetchone()[0]
            if result['kind'] in ('likes', 'recommendations'):
                result['unknown_authors'] = con.execute("SELECT COUNT(DISTINCT author_key) FROM liked_videos WHERE task_id=? AND author_douyin_id=''", (task_id,)).fetchone()[0]
            result['collection_complete'] = bool(result['collection_complete'])
            result['verify_authors'] = bool(result['verify_authors'])
            result['verification_complete'] = bool(result['collection_complete'] and result['unknown_authors'] == 0)
            result.pop('stop_requested', None)
            return result

    def list_tasks(self):
        with self.connection() as con:
            # Aggregate each child table once; avoid opening and recounting every
            # historical task separately during the frontend's polling loop.
            rows = con.execute('''SELECT t.*,
                COALESCE(c.main_count,0) AS main_count,
                COALESCE(c.reply_count,0) AS reply_count,
                COALESCE(v.video_count,0) AS video_count,
                CASE WHEN t.kind IN ('likes','recommendations')
                    THEN COALESCE(v.unknown_authors,0)
                    ELSE COALESCE(a.unknown_authors,0) END AS unknown_authors
                FROM tasks t
                LEFT JOIN (
                    SELECT task_id,SUM(parent_id='') AS main_count,
                        SUM(parent_id!='') AS reply_count FROM comments GROUP BY task_id
                ) c ON c.task_id=t.id
                LEFT JOIN (
                    SELECT task_id,COUNT(*) AS unknown_authors FROM authors a
                    WHERE (identity_state!='verified' OR douyin_id='')
                        AND EXISTS(SELECT 1 FROM comments c WHERE c.task_id=a.task_id AND c.author_key=a.author_key)
                    GROUP BY task_id
                ) a ON a.task_id=t.id
                LEFT JOIN (
                    SELECT task_id,COUNT(*) AS video_count,
                        COUNT(DISTINCT CASE WHEN author_douyin_id='' THEN author_key END) AS unknown_authors
                    FROM liked_videos GROUP BY task_id
                ) v ON v.task_id=t.id
                ORDER BY t.created_at DESC''').fetchall()
        results = []
        for row in rows:
            task = dict(row)
            task['collection_complete'] = bool(task['collection_complete'])
            task['verify_authors'] = bool(task['verify_authors'])
            task['verification_complete'] = bool(task['collection_complete'] and task['unknown_authors'] == 0)
            task.pop('stop_requested', None)
            results.append(task)
        return results

    def update_task(self, task_id, **fields):
        allowed = {'source_url', 'video_id', 'title', 'status', 'error', 'stop_requested', 'collection_complete', 'verification_complete',
                   'target_sec_uid', 'target_nickname', 'visibility', 'verify_authors'}
        if 'verify_authors' in fields and type(fields['verify_authors']) is not bool:
            raise ValueError('Invalid author verification option')
        if not fields.keys() <= allowed:
            raise ValueError('Unknown task fields: ' + ','.join(fields.keys() - allowed))
        fields['updated_at'] = time.time()
        with self.connection() as con:
            con.execute('UPDATE tasks SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', (*fields.values(), task_id))

    def should_stop(self, task_id):
        with self.connection() as con:
            row = con.execute('SELECT stop_requested FROM tasks WHERE id=?', (task_id,)).fetchone()
            return row is None or bool(row[0])

    def delete_task(self, task_id):
        with self.connection() as con:
            con.execute('DELETE FROM tasks WHERE id=?', (task_id,))

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
        con.execute('INSERT OR REPLACE INTO authors(task_id,author_key,uid,sec_uid,douyin_id,nickname,identity_state) VALUES(?,?,?,?,?,?,?)',
                    (task_id, author_key, *(merged[k] for k in ('uid','sec_uid','douyin_id','nickname','identity_state'))))
        return merged

    def save_author(self, task_id, author_key, **fields):
        with self.connection() as con:
            self._save_author(con, task_id, author_key, profile_verified=fields.get('identity_state') == 'verified', **fields)
            con.execute('UPDATE tasks SET updated_at=? WHERE id=?', (time.time(),task_id))

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
        COALESCE(NULLIF(a.douyin_id,''),c.douyin_id) AS public_number,
        COALESCE(NULLIF(c.nickname,''),a.nickname,'') AS display_name,
        COALESCE(a.identity_state,c.identity_state) AS resolved_state
        FROM comments c LEFT JOIN authors a ON c.task_id=a.task_id AND c.author_key=a.author_key'''

    def read_comment_page(self, task_id, filters, *, limit, offset=0):
        from .search import format_comment, match_text
        if type(limit) is not int or limit < 0 or type(offset) is not int or offset < 0:
            raise ValueError('Invalid comment page')
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
        query = self.COMMENT_SELECT + where
        order = ' ORDER BY c.created_at IS NULL,c.created_at DESC,c.comment_id DESC'
        with self.connection() as con:
            con.execute('BEGIN')
            missing = con.execute('SELECT COUNT(*) FROM comments WHERE task_id=? AND created_at IS NULL', (task_id,)).fetchone()[0]
            if not filters.keyword:
                total = con.execute('SELECT COUNT(*) FROM (' + query + ')', params).fetchone()[0]
                rows = con.execute(query + order + ' LIMIT ? OFFSET ?', (*params, min(limit, 2**63-1), min(offset, 2**63-1)))
                items = [format_comment(dict(row)) for row in rows]
            else:
                total, items = 0, []
                cursor = con.execute(query + order, params)
                while batch := cursor.fetchmany(256):
                    for row in batch:
                        matched, kind, score = match_text(row['text'], filters)
                        if matched:
                            if offset <= total < offset + limit:
                                items.append(format_comment(dict(row), kind, score))
                            total += 1
        return items, total, missing

    def read_comments(self, task_id):
        # A single SQL read yields a consistent export/search snapshot, including hydrated identity.
        with self.connection() as con:
            return [dict(r) for r in con.execute('''SELECT c.*,
                COALESCE(NULLIF(a.douyin_id,''),c.douyin_id) AS public_number,
                COALESCE(NULLIF(c.nickname,''),a.nickname,'') AS display_name,
                COALESCE(a.identity_state,c.identity_state) AS resolved_state
                FROM comments c LEFT JOIN authors a ON c.task_id=a.task_id AND c.author_key=a.author_key
                WHERE c.task_id=? ORDER BY c.created_at IS NULL,c.created_at DESC,c.comment_id DESC''', (task_id,))]

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
            next_order = con.execute('SELECT COALESCE(MAX(collected_order),0) FROM liked_videos WHERE task_id=?', (task_id,)).fetchone()[0] + 1
            for video in videos:
                values = [video.get(k, None if k in ('created_at', 'action_at') else '') for k in cols]
                con.execute('INSERT INTO liked_videos(task_id,' + ','.join(cols) + ') VALUES(' + ','.join('?' for _ in range(len(cols)+1)) + ') ON CONFLICT(task_id,video_id) DO UPDATE SET ' + ','.join(updates), (task_id, *values))
                con.execute('UPDATE liked_videos SET collected_order=? WHERE task_id=? AND video_id=? AND collected_order IS NULL', (next_order, task_id, video['video_id']))
                next_order += 1
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
            total = con.execute('SELECT COUNT(*) FROM liked_videos WHERE task_id=?', (task_id,)).fetchone()[0]
            rows = self._read_liked_videos(con, task_id, limit=limit, offset=offset)
        return rows, total

    @staticmethod
    def _read_liked_videos(con, task_id, *, limit=None, offset=0):
        from .search import time_text
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError('Invalid video page limit')
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid video page offset')
        task = con.execute('SELECT list_order FROM tasks WHERE id=?', (task_id,)).fetchone()
        order = 'collected_order IS NULL,collected_order ASC,created_at DESC,video_id DESC' if task and task['list_order'] == 'activity' else 'created_at IS NULL,created_at DESC,video_id DESC'
        sql = 'SELECT * FROM liked_videos WHERE task_id=? ORDER BY ' + order
        params = (task_id,)
        if limit is not None or offset:
            sql += ' LIMIT ? OFFSET ?'
            # Arbitrarily large API page numbers should remain empty pages,
            # rather than overflowing SQLite's signed integer binding.
            params += (min(limit, 2**63 - 1) if limit is not None else -1, min(offset, 2**63 - 1))
        rows = [dict(r) for r in con.execute(sql, params)]
        for row in rows:
            row['time_text'] = time_text(row['created_at'])
        return rows

    def liked_authors(self, task_id):
        with self.connection() as con:
            return [dict(r) for r in con.execute("SELECT author_key,MAX(uid) AS uid,MAX(sec_uid) AS sec_uid FROM liked_videos WHERE task_id=? AND author_douyin_id='' GROUP BY author_key", (task_id,))]

    def cached_account_profiles(self, account):
        # Previously verified public profiles are discovery hints only. The
        # collector must fetch and reverify the profile before using a hint.
        with self.connection() as con:
            return [dict(r) for r in con.execute("""SELECT DISTINCT uid,sec_uid FROM authors
                WHERE identity_state='verified' AND sec_uid!='' AND douyin_id=? COLLATE NOCASE
                UNION SELECT DISTINCT uid,sec_uid FROM liked_videos
                WHERE sec_uid!='' AND author_douyin_id=? COLLATE NOCASE""", (account, account))]

    def save_liked_author(self, task_id, author_key, author_name='', author_douyin_id=''):
        with self.connection() as con:
            con.execute("UPDATE liked_videos SET author_name=COALESCE(NULLIF(author_name,''),?),author_douyin_id=? WHERE task_id=? AND author_key=?", (author_name, author_douyin_id, task_id, author_key))
            con.execute('UPDATE tasks SET updated_at=? WHERE id=?', (time.time(), task_id))
