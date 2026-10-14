"""A user's recommended works, distinct from likes and the home feed."""
from __future__ import annotations

import math
from typing import Any

from backend.likes import LikesCollector


def recommendation_time(raw: dict, target: dict) -> int | None:
    """Read only the target user's recommendation label, in Unix seconds.

    No fallback to create_time, another friend's label, or aweme_date.date_map:
    the latter's meaning is not established. Conflicting labels stay unknown.
    """
    info = raw.get('friend_recommend_info')
    labels = info.get('label_user_list') if isinstance(info, dict) else None
    if not isinstance(labels, list):
        return None
    found = set()
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get('user'), dict):
            continue
        user = label['user']
        pairs = [(str(target.get(key) or ''), str(user.get(key) or '')) for key in ('sec_uid', 'uid')]
        shared = [(a, b) for a, b in pairs if a and b]
        if not shared or any(a != b for a, b in shared):
            continue
        value = label.get('recommend_time')
        if isinstance(value, bool):
            return None
        try:
            stamp = float(value)
            if not math.isfinite(stamp) or stamp != int(stamp) or not 0 < stamp < 253402300800:
                return None
        except (ValueError, TypeError, OverflowError):
            return None
        found.add(int(stamp))
    return next(iter(found)) if len(found) == 1 else None


def explicit_recommendations_private(response: Any) -> bool:
    """Accept only a direct list-specific denial, never a guessed numeric flag.

    These are conservative text matches, not a claim that the platform always
    supplies a privacy message. Missing lists and unknown codes remain errors.
    """
    if not isinstance(response, dict) or response.get('verify_check') or response.get('verify_info'):
        return False
    if response.get('aweme_list'):
        return False
    message = str(response.get('status_msg') or response.get('message') or '').strip().rstrip('。.!！')
    return message in {
        '该账号未公开推荐列表', '该用户未公开推荐列表', '用户未公开推荐列表',
        '推荐列表未公开', '推荐列表为私密', '该用户的推荐列表为私密',
        '该账号的推荐列表为私密', '推荐列表仅自己可见',
        '该用户已将推荐列表设为私密', '该用户已将推荐列表设置为私密',
    }


class RecommendationsCollector(LikesCollector):
    list_kind = 'recommendations'
    list_label = '推荐'

    def target_visibility(self, user: dict) -> str:
        # show_favorite_list controls likes only. No independently verified
        # recommendation permission flag is currently available on profiles.
        return 'unknown'

    def list_is_private(self, response: Any) -> bool:
        return explicit_recommendations_private(response)

    def action_time(self, raw: dict) -> int | None:
        return recommendation_time(raw, getattr(self, 'target_identity', {}))

    async def fetch_list_page(self, sec_uid: str, cursor: int) -> dict:
        return await self.client.get_user_recommendations(sec_uid, cursor, count=self.request_count())
