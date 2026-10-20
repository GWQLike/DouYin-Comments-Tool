"""Per-result identity checks shared by comments and both video lists."""
import asyncio
import time

from .crawler import ManualActionRequired, PageFailure, StopRequested, normalize_profile
from .responses import LoginRequired, RequestRejected, NetworkPause


def sync_record(con, task_id, record_id, author_key, uid, sec_uid, number, nickname, conflict=False):
    source = (author_key or '', uid or '', sec_uid or '', number or '', int(conflict))
    old = con.execute('''SELECT author_key,source_uid,source_sec_uid,source_number,source_conflict,status,nickname
        FROM author_verifications WHERE task_id=? AND record_id=?''', (task_id, record_id)).fetchone()
    same_identity = old and tuple(old)[:4] == source[:4]
    if same_identity and (old[4] == source[4] or (old[4] and not conflict and old[5] == 'confirmed')):
        if old[4] != source[4] or (not old[6] and nickname):
            con.execute('''UPDATE author_verifications SET nickname=COALESCE(NULLIF(nickname,''),?),source_conflict=?
                WHERE task_id=? AND record_id=?''', (nickname or '', int(conflict), task_id, record_id))
        return
    status = 'conflict' if conflict else 'confirmed' if number else 'pending'
    con.execute('''INSERT INTO author_verifications(task_id,record_id,author_key,source_uid,source_sec_uid,
        source_number,source_conflict,uid,sec_uid,douyin_id,nickname,status,failure_kind)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id,record_id) DO UPDATE SET
        author_key=excluded.author_key,source_uid=excluded.source_uid,source_sec_uid=excluded.source_sec_uid,
        source_number=excluded.source_number,source_conflict=excluded.source_conflict,uid=excluded.uid,
        sec_uid=excluded.sec_uid,douyin_id=excluded.douyin_id,nickname=excluded.nickname,
        status=excluded.status,failure_kind=excluded.failure_kind''',
        (task_id, record_id, *source, uid or '', sec_uid or '', number or '', nickname or '', status,
         'identity_conflict' if conflict else ''))


def profile_result(response, expected):
    user = response.get('user') if isinstance(response, dict) else None
    if not isinstance(user, dict):
        return 'request_failed', 'malformed', None
    for key in ('uid', 'sec_uid'):
        actual = str(user.get(key) or '')
        if actual not in ('', '0') and expected.get(key) and actual != expected[key]:
            return 'conflict', 'identity_conflict', None
    fields = normalize_profile(response, expected)
    if fields is None:
        return 'not_provided', 'identity_missing', None
    if not fields['douyin_id']:
        return 'not_provided', 'public_number_missing', fields
    return 'confirmed', '', fields


def failure_kind(exc):
    if isinstance(exc, LoginRequired):
        return 'login'
    if isinstance(exc, ManualActionRequired):
        return 'verification'
    if isinstance(exc, NetworkPause):
        return 'temporary'
    kind = getattr(exc, 'kind', None)
    if kind:
        return getattr(kind, 'value', kind)
    if isinstance(exc, (StopRequested, asyncio.CancelledError)):
        return 'cancelled'
    return 'request_failed'


async def hydrate(task):
    task._verifying = True
    task.db.update_task(task.task_id, status='verifying')
    records = task.db.verification_records(task.task_id)
    # Only raw identities supplied with collected results can provide aliases.
    # A saved conclusion from another record is never copied without checking.
    known = {}
    for record in records:
        if record['source_number'] and not record['source_conflict']:
            raw = {'user': {'uid': record['source_uid'], 'sec_uid': record['source_sec_uid'],
                            'unique_id': record['source_number'], 'nickname': record['nickname']}}
            for key in ('uid', 'sec_uid'):
                if raw['user'][key]:
                    known.setdefault((key, raw['user'][key]), []).append(raw)
    write = getattr(task, 'write', lambda operation, *args, **kwargs: operation(*args, **kwargs))

    groups = {}
    for record in records:
        if record['status'] != 'confirmed':
            groups.setdefault(record['source_sec_uid'] or record['sec_uid'], []).append(record)

    async def check(group):
        queued, results = [], []
        try:
            for record in group:
                task.check_stop()
                expected = {**record, 'uid': record['source_uid'] or record['uid'],
                            'sec_uid': record['source_sec_uid'] or record['sec_uid']}
                if not record['source_conflict']:
                    candidates = [profile_result(raw, expected) for key in ('uid', 'sec_uid')
                                  for raw in known.get((key, expected[key]), [])]
                    candidates = [fields for status, _, fields in candidates if status == 'confirmed']
                    if len({fields['douyin_id'] for fields in candidates}) == 1:
                        results.append((record, 'confirmed', '', candidates[0]))
                        continue
                if not expected['sec_uid']:
                    results.append((record, 'no_query', 'missing_sec_uid', None))
                else:
                    queued.append(record)
        finally:
            if results:
                write(task.db.finish_verifications, task.task_id, results)
        if not queued:
            return
        task.check_stop()
        write(task.db.begin_verifications, task.task_id, queued)
        try:
            response = await task.author_profile(queued[0]['source_sec_uid'] or queued[0]['sec_uid'])
        except (PageFailure, RequestRejected) as exc:
            write(task.db.finish_verifications, task.task_id,
                  [(record, 'request_failed', failure_kind(exc), None) for record in queued])
            return
        except (StopRequested, ManualActionRequired, asyncio.CancelledError) as exc:
            write(task.db.finish_verifications, task.task_id,
                  [(record, 'request_failed', failure_kind(exc), None) for record in queued])
            raise
        results = []
        try:
            for record in queued:
                task.check_stop()
                expected = {**record, 'uid': record['source_uid'] or record['uid'],
                            'sec_uid': record['source_sec_uid'] or record['sec_uid']}
                status, reason, fields = profile_result(response, expected)
                results.append((record, status, reason, fields))
        finally:
            if results:
                write(task.db.finish_verifications, task.task_id, results)

    began = time.perf_counter()
    try:
        await task.parallel(groups.values(), check)
    finally:
        task.metrics['author_seconds'] += time.perf_counter() - began
