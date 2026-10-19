"""Raw-page adapter; database records and cursor advance share one transaction."""
from backend.crawler import PageFailure, normalize_comment
from backend.likes import normalize_liked_video
from backend.responses import validate_result


class CommentPages:
    page_error = PageFailure

    def __init__(self, task, video_id, parent_id=''):
        self.task, self.video_id, self.parent_id = task, video_id, parent_id
        self.request, self.check_stop = task.request, task.check_stop

    def checkpoint(self):
        return self.task.db.get_checkpoint(self.task.task_id, self.parent_id)

    def normalize(self, response, cursor):
        response = validate_result(response)
        if response.get('has_more') not in (0, 1, '0', '1', False, True):
            raise PageFailure('平台未返回有效分页结束标志，已保留断点。')
        done = response['has_more'] in (0, '0', False)
        raw = response.get('comments')
        if raw is None and done:
            raw = []
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise PageFailure('平台评论列表格式异常，已保留断点。')
        next_cursor = response.get('cursor', cursor)
        if not str(next_cursor).isdigit():
            raise PageFailure('平台返回的分页游标无效，已保留断点。')
        rows = []
        for item in raw:
            normalized = normalize_comment(item, self.video_id, self.parent_id)
            rows.append(normalized)
            previews = item.get('reply_comment')
            if previews is None:
                continue
            if not isinstance(previews, list) or any(not isinstance(reply, dict) for reply in previews):
                raise PageFailure('平台内嵌回复格式异常，已保留断点。')
            rows.extend(normalize_comment(reply, self.video_id, self.parent_id or normalized['comment_id'], preview=True) for reply in previews)
        return {'rows': rows, 'done': done, 'next_cursor': int(next_cursor), 'has_cursor': 'cursor' in response}

    def filter(self, rows, done):
        return rows

    def commit(self, rows, cursor, done):
        self.task.write(self.task.db.save_page, self.task.task_id, self.parent_id, rows, cursor, done)


class ActivityPages:
    page_error = PageFailure

    def __init__(self, task, visibility):
        self.task, self.visibility = task, visibility
        self.request, self.check_stop = task.request, task.check_stop
        self.commit = task.save_list_page

    def checkpoint(self):
        return self.task.db.get_checkpoint(self.task.task_id, self.task.list_kind)

    def private(self):
        self.task.db.update_task(self.task.task_id, visibility='private', status='private',
            collection_complete=False, error=f'该账号未公开{self.task.list_label}列表')

    def normalize(self, response, cursor):
        if self.task.list_is_private(response):
            return {'private': True}
        response = validate_result(response)
        if response.get('has_more') not in (0, 1, '0', '1', False, True):
            raise PageFailure(f'{self.task.list_label}列表未返回有效分页结束标志，已保留断点。')
        raw = response.get('aweme_list')
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise PageFailure(f'{self.task.list_label}列表未返回有效视频列表，无法判断是否公开，已保留断点。')
        next_cursor = response.get('max_cursor', cursor)
        if not str(next_cursor).isdigit():
            raise PageFailure(f'{self.task.list_label}列表分页游标无效，已保留断点。')
        return {'rows': [{**normalize_liked_video(row), 'action_at': self.task.action_time(row)} for row in raw],
                'done': response['has_more'] in (0, '0', False), 'next_cursor': int(next_cursor), 'has_cursor': 'max_cursor' in response}

    def filter(self, rows, done):
        if rows:
            self.visibility = 'public'
            self.task.db.update_task(self.task.task_id, visibility='public')
        rows = self.task.filter_list_rows(rows)
        if done and self.visibility != 'public':
            raise PageFailure(f'未获得视频且平台未明确说明{self.task.list_label}列表是否公开，暂时无法确认；请稍后重试。')
        return rows
