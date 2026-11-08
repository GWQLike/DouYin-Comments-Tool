"""Interpret explicit list visibility and target-user activity timestamps."""
from __future__ import annotations

import math


def profile_visibility(user: dict) -> str:
    # The profile schema defines this as an optional boolean. In particular,
    # None/0/"false" and guessed favorite_permission codes are not evidence.
    flag = user.get("show_favorite_list")
    if flag is False:
        return "private"
    if flag is True:
        return "public"
    return "unknown"


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
