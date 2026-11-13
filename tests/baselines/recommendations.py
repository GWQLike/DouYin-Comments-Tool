"""A user's recommended works, distinct from likes and the home feed."""
from __future__ import annotations

from typing import Any

from .likes import LikesCollector
from backend.activity import recommendation_time


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
