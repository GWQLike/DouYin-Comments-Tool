"""Workbench compatibility v1: explicit resumable page hooks.

No storage implementation, sleeps or retries are hidden here. A page is committed
by the host before its cursor advances. Failure/cancellation never marks done.
"""
async def collect_pages(fetch, hooks):
    checkpoint = hooks.checkpoint()
    if checkpoint.get('done'):
        return
    cursor = checkpoint.get('cursor', 0)
    resumed, restarted, stalls = bool(cursor), False, 0
    visited = {str(cursor)}
    while True:
        hooks.check_stop()

        async def request_page():
            raw = await fetch(cursor)
            return hooks.normalize(raw, cursor)

        try:
            page = await hooks.request(request_page)
        except hooks.page_error:
            if resumed and not restarted:
                hooks.restart()
                cursor, visited, stalls, restarted = 0, {'0'}, 0, True
                continue
            raise
        if page.get('private'):
            hooks.private()
            return
        rows, done, next_cursor = page['rows'], page['done'], page['next_cursor']
        raw_count = len(rows)
        rows = hooks.filter(rows, done)
        if done:
            hooks.commit(rows, next_cursor, True)
            return
        if not raw_count or not page['has_cursor'] or str(next_cursor) in visited:
            hooks.commit(rows, cursor, False)
            stalls += 1
            if stalls >= 3:
                if resumed and not restarted:
                    hooks.restart()
                    cursor, visited, stalls, restarted = 0, {'0'}, 0, True
                    continue
                raise hooks.page_error('连续三次空页或分页游标停滞，采集尚未完成，可稍后继续。')
            continue
        hooks.commit(rows, next_cursor, False)
        cursor, stalls = next_cursor, 0
        visited.add(str(cursor))
