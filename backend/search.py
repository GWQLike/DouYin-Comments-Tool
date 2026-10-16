from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import lru_cache
import unicodedata
from zoneinfo import ZoneInfo

from opencc import OpenCC
from rapidfuzz.fuzz import partial_ratio

SHANGHAI = ZoneInfo('Asia/Shanghai')
CONVERTERS = tuple(OpenCC(c) for c in ('t2s', 'tw2s', 'hk2s'))


@lru_cache(maxsize=20000)
def search_keys(text):
    text = unicodedata.normalize('NFKC', text).casefold().strip()
    return tuple(dict.fromkeys(c.convert(text) for c in CONVERTERS))


def date_bound(value, end=False):
    if not value:
        return None
    try:
        value = value.strip()
        d = datetime.fromisoformat(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=SHANGHAI)
        if len(value) == 10 and end:
            d = datetime.combine(d.date() + timedelta(days=1), time.min, SHANGHAI) - timedelta(microseconds=1)
        return d.timestamp()
    except (ValueError, OverflowError):
        raise ValueError('时间格式无效，请使用日期或日期与时间') from None


@dataclass
class Filters:
    account: str = ''
    keyword: str = ''
    mode: str = 'contains'
    threshold: float = 85
    start: str = ''
    end: str = ''

    def __post_init__(self):
        self.account = self.account.strip().removeprefix('@')
        self.keyword = self.keyword.strip()
        if self.mode not in ('contains','fuzzy'):
            raise ValueError('不支持的搜索模式')
        if not 70 <= self.threshold <= 100:
            raise ValueError('近似匹配阈值必须在 70–100 之间')
        self.start_ts, self.end_ts = date_bound(self.start), date_bound(self.end, True)
        if self.start_ts is not None and self.end_ts is not None and self.start_ts > self.end_ts:
            raise ValueError('开始时间不能晚于结束时间')


def match_text(text, filters):
    if not filters.keyword:
        return True, None, None
    queries, contents = search_keys(filters.keyword), search_keys(text)
    if any(q in c for q in queries for c in contents):
        return True, 'contains', 100.0
    if filters.mode == 'fuzzy' and min(map(len, queries)) >= 4:
        score = max((partial_ratio(q,c,processor=None,score_cutoff=filters.threshold) for q in queries for c in contents if len(c)>=len(q)), default=0)
        if score >= filters.threshold:
            return True, 'fuzzy', round(score,1)
    return False, None, None


def time_text(timestamp):
    if timestamp is None:
        return '未提供'
    try:
        return datetime.fromtimestamp(timestamp, SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OverflowError, OSError):
        return '未提供'


def format_comment(row, kind=None, score=None):
    return {k:row[k] for k in ('comment_id','video_id','parent_id','text','created_at','ip_label')} | {
        'nickname':row.get('display_name') or '',
        'douyin_id':row.get('public_number') if row.get('resolved_state') == 'verified' else '',
        'time_text':time_text(row['created_at']),'match_type':kind,'score':score}


def select_comments(rows, filters):
    result = []
    # Public identity must be verified. Bind all rows of the same author after a verified hit.
    target_keys = {r['author_key'] for r in rows if r.get('resolved_state') == 'verified' and r.get('public_number') == filters.account} if filters.account else set()
    for row in rows:
        if filters.account and row['author_key'] not in target_keys:
            continue
        ts = row['created_at']
        if filters.start_ts is not None and (ts is None or ts < filters.start_ts):
            continue
        if filters.end_ts is not None and (ts is None or ts > filters.end_ts):
            continue
        matched, kind, score = match_text(row['text'], filters)
        if not matched:
            continue
        result.append(format_comment(row, kind, score))
    return result


def export_txt(items, collection_complete=True):
    return export_records(items, (
        ('用户名','nickname'),('抖音号','douyin_id'),('评论内容','text'),('时间','time_text'),('IP 属地','ip_label')),
        collection_complete)


def export_likes_txt(items, collection_complete=True):
    return export_records(items, (
        ('视频标题','title'),('作者名称','author_name'),('作者抖音号','author_douyin_id'),
        ('发布时间','time_text'),('视频链接','video_url')), collection_complete)


def export_records(items, fields, collection_complete=True):
    def clean(value):
        return str(value or '未提供').replace('\\','\\\\').replace('\r\n','\n').replace('\r','\n').replace('\n','\\n')
    blocks = []
    if not collection_complete:
        blocks.append('采集尚未完成，仅导出当前已获取的匹配结果。')
    for item in items:
        blocks.append('\r\n'.join(f'{label}：{clean(item.get(field))}' for label,field in fields))
    return ('\r\n\r\n'.join(blocks) + ('\r\n' if blocks else '')).encode('utf-8-sig')
