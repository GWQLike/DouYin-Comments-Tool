"""Reject unsupported collection conditions before opening a platform session."""
from backend.crawler import PageFailure


class PreflightRejected(PageFailure):
    pass


def check_task_conditions(task, *, verify_only=False):
    # Author completion only uses saved results, not the collection time range.
    if verify_only:
        return
    if task['kind'] == 'likes' and any(task.get(key) is not None for key in ('start_time', 'end_time')):
        raise PreflightRejected(
            '当前版本无法获取可靠的点赞时间，不支持按点赞时间范围采集；'
            '请新建不限制时间的任务，可通过采集数量获取最近喜欢的视频。'
        )
