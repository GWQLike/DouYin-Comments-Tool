"""Workbench compatibility v2: preserve cursors without proven invalidation.

The host owns storage, pacing and request retry budgets. A page is committed
before its cursor advances. Failure/cancellation never marks done.
"""
import hashlib


async def collect_pages(fetch, hooks):
    checkpoint = hooks.checkpoint()
    if checkpoint.get('done'):
        return
    cursor = checkpoint.get('cursor', 0)
    stalls = 0
    visited = {str(cursor)}
    pages = set()
    while True:
        hooks.check_stop()

        async def request_page():
            raw = await fetch(cursor)
            return hooks.normalize(raw, cursor)

        # No verified cursor-invalid response exists for the pinned endpoints.
        # Transport/shape failures must keep the last committed position.
        page = await hooks.request(request_page)
        hooks.check_stop()
        if page.get('private'):
            hooks.private()
            return
        rows, done, next_cursor = page['rows'], page['done'], page['next_cursor']
        raw_count = len(rows)
        identifiers = sorted({str(row.get('comment_id') or row.get('video_id') or '') for row in rows})
        fingerprint = hashlib.sha256('\0'.join(identifiers).encode('utf-8')).digest()
        rows = hooks.filter(rows, done)
        hooks.check_stop()
        if done:
            hooks.commit(rows, next_cursor, True)
            return
        if not raw_count or not page['has_cursor'] or str(next_cursor) in visited or fingerprint in pages:
            hooks.commit(rows, cursor, False)
            stalls += 1
            if stalls >= 3:
                raise hooks.page_error('连续三次空页、重复页或分页游标停滞，无法确认游标已失效；已保留原断点，可稍后继续。')
            continue
        hooks.commit(rows, next_cursor, False)
        cursor, stalls = next_cursor, 0
        visited.add(str(cursor))
        pages.add(fingerprint)
