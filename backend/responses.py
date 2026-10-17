"""Response categories and safe errors shared by the transport and task retry loop."""
from dataclasses import dataclass
from enum import Enum

from backend.crawler import ControlledStop, ManualActionRequired, PageFailure


class Kind(str, Enum):
    SUCCESS = 'success'
    LOGIN = 'login'
    VERIFICATION = 'verification'
    RATE_LIMIT = 'rate_limit'
    TEMPORARY = 'temporary'
    PERMANENT = 'permanent'
    PRIVATE = 'private'
    MALFORMED = 'malformed'


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    reason: str = ''
    retry_after: str | None = None


class RequestFailure(PageFailure):
    """An already-accounted transport result; do not apply backoff a second time."""
    def __init__(self, outcome):
        super().__init__(outcome.reason)
        self.outcome = outcome
        self.retryable = outcome.kind in (Kind.TEMPORARY, Kind.RATE_LIMIT, Kind.MALFORMED)


class LoginRequired(ManualActionRequired):
    pass


class VerificationRequired(ManualActionRequired):
    pass


class RequestRejected(Exception):
    """A terminal operation error, outside the paginator's replayable errors."""


class NetworkPause(ControlledStop):
    def __init__(self):
        super().__init__('网络或平台服务连续 5 次暂时故障，任务已暂停；断点已保留，请稍后继续。')


MALFORMED = Outcome(Kind.MALFORMED, '平台响应结构异常，无法确认本页结果，已保留断点。')
TEMPORARY = Outcome(Kind.TEMPORARY, '网络或平台服务暂时不可用，已降速并保留断点。')
LOGIN = Outcome(Kind.LOGIN, '登录已失效，请在专用浏览器重新登录。')
VERIFICATION = Outcome(Kind.VERIFICATION, '平台要求人工验证，请在专用浏览器处理后继续。')


def explicit_list_private(data, kind):
    if kind not in ('likes', 'recommendations') or not isinstance(data, dict):
        return False
    if data.get('verify_check') or data.get('verify_info') or data.get('aweme_list'):
        return False
    label = '喜欢' if kind == 'likes' else '推荐'
    message = str(data.get('status_msg') or data.get('message') or '').strip().rstrip('。.!！')
    return message in {
        f'该账号未公开{label}列表', f'该用户未公开{label}列表', f'用户未公开{label}列表',
        f'{label}列表未公开', f'{label}列表为私密', f'该用户的{label}列表为私密',
        f'该账号的{label}列表为私密', f'{label}列表仅自己可见',
        f'该用户已将{label}列表设为私密', f'该用户已将{label}列表设置为私密',
    }


def classify_payload(data, *, category=None):
    if not isinstance(data, dict):
        return MALFORMED
    if data.get('verify_check') or data.get('verify_info'):
        return VERIFICATION
    nil = data.get('search_nil_info')
    signals = {str(nil.get(key, '')).lower() for key in ('search_nil_type', 'search_nil_item')} if isinstance(nil, dict) else set()
    if signals & {'verify_check', 'verify', 'captcha'}:
        return VERIFICATION
    if 'login' in signals:
        return LOGIN
    message = ' '.join(str(data.get(key) or '') for key in ('status_msg', 'message')).lower()
    if any(term in message for term in ('验证码', '人工验证', '人机验证', 'captcha', 'verify', 'verification')):
        return VERIFICATION
    if any(term in message for term in ('登录', 'login', 'log in')):
        return LOGIN
    # Preserve the existing login code; other unknown numeric codes alone are
    # not evidence of a captcha or a private list.
    code = data.get('status_code', 0)
    if code in (1001, '1001'):
        return LOGIN
    if explicit_list_private(data, category):
        return Outcome(Kind.PRIVATE)
    if any(term in message for term in ('请求过于频繁', '访问过于频繁', 'too many requests', 'rate limit')):
        return Outcome(Kind.RATE_LIMIT, '平台请求频率受限，已进入冷却。')
    if any(term in message for term in ('系统繁忙', '服务暂时不可用', '服务异常', 'service unavailable', 'temporarily unavailable')):
        return TEMPORARY
    if code not in (0, '0', None):
        return Outcome(Kind.PERMANENT, '平台返回不可重试或未识别的失败状态，已停止本次操作并保留断点。')
    # These endpoints unwrap or inspect fields outside the page normalizer.
    # Validate their envelope before recording a successful request.
    for name, field in (('video_detail', 'aweme_detail'), ('author_profile', 'user')):
        if category == name and not isinstance(data.get(field), dict):
            return MALFORMED
    if category == 'user_search':
        users = data.get('user_list')
        if not isinstance(users, list) or any(not isinstance(item, dict) for item in users):
            return MALFORMED
        if data.get('has_more') not in (0, 1, '0', '1', False, True):
            return MALFORMED
    return Outcome(Kind.SUCCESS)


def classify_http(status, data, *, category=None, raw=False, retry_after=None):
    # Status takes precedence over malformed bodies. Only an explicit structured
    # denial can turn a 403 into login, captcha, rate limiting or list privacy.
    if status == 429:
        return Outcome(Kind.RATE_LIMIT, '平台请求频率受限，已进入冷却。', retry_after)
    if status == 401:
        return LOGIN
    if status in (408, 425) or status >= 500:
        return TEMPORARY
    if status == 403:
        business = classify_payload(data, category=category)
        if business.kind == Kind.RATE_LIMIT:
            return Outcome(business.kind, business.reason, retry_after)
        if business.kind in (Kind.LOGIN, Kind.VERIFICATION, Kind.PRIVATE):
            return business
        return Outcome(Kind.PERMANENT, '平台拒绝访问（HTTP 403），原因尚未明确；已停止本次操作并保留断点。')
    if status >= 400:
        return Outcome(Kind.PERMANENT, f'平台请求未成功（HTTP {status}），已停止本次操作并保留断点。')
    if raw:
        return Outcome(Kind.SUCCESS)
    business = classify_payload(data, category=category)
    return Outcome(business.kind, business.reason, retry_after) if business.kind == Kind.RATE_LIMIT else business


def validate_result(data):
    """Validate a normalized task result without counting another HTTP outcome."""
    outcome = classify_payload(data)
    if outcome.kind == Kind.LOGIN:
        raise LoginRequired(outcome.reason)
    if outcome.kind == Kind.VERIFICATION:
        raise VerificationRequired(outcome.reason)
    if outcome.kind != Kind.SUCCESS:
        # No transport boundary (e.g. a simulated task client) has accounted this.
        raise PageFailure(outcome.reason)
    return data
