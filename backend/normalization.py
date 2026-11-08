"""Normalize collected records without importing collectors or starting sessions."""
from __future__ import annotations

import math
import re
from typing import Any

from backend.errors import PageFailure


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_comment(raw: dict, video_id: str, parent_id: str = "", *, preview: bool = False) -> dict:
    """Do not confuse internal uid/legacy short_id with an asserted public Douyin ID."""
    comment_id = _string(raw.get("cid") or raw.get("comment_id"))
    if not comment_id:
        raise PageFailure("平台返回缺少评论 ID 的记录，本页未确认完成。")
    user = raw.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    uid = _string(user.get("uid"))
    sec_uid = _string(user.get("sec_uid"))
    uid = "" if uid == "0" else uid
    sec_uid = "" if sec_uid == "0" else sec_uid
    public_id = _string(user.get("unique_id")).strip()
    public_id = "" if public_id == "0" else public_id
    stamp = raw.get("create_time")
    try:
        number = float(stamp)
        stamp = int(number) if math.isfinite(number) and 0 < number < 253402300800 else None
    except (ValueError, TypeError, OverflowError):
        stamp = None
    try:
        # A missing count is not evidence that there are no replies. Query that
        # thread once; an explicit zero needs no additional request.
        reply_count = max(0, int(raw.get("reply_comment_total", 1)))
    except (ValueError, TypeError):
        reply_count = 1
    if raw.get("reply_comment"):
        reply_count = max(reply_count, 1)
    present = {field for field, source in (
        ('text', 'text'), ('created_at', 'create_time'), ('ip_label', 'ip_label'),
        ('reply_count', 'reply_comment_total')) if source in raw}
    present.update(field for field, source in (
        ('uid', 'uid'), ('sec_uid', 'sec_uid'), ('nickname', 'nickname'),
        ('douyin_id', 'unique_id')) if source in user)
    if raw.get('reply_comment'):
        present.add('reply_count')
    # A null/non-text value is unavailable, not an explicit empty edit.
    if not isinstance(raw.get('text'), str):
        present.discard('text')
    return {
        "comment_id": comment_id,
        "video_id": _string(video_id),
        "parent_id": _string(parent_id),
        "text": _string(raw.get("text")),
        "created_at": stamp,
        "ip_label": _string(raw.get("ip_label")),
        "nickname": _string(user.get("nickname")),
        "douyin_id": public_id,
        "uid": uid,
        "sec_uid": sec_uid,
        "author_key": uid or sec_uid or f"comment:{comment_id}",
        "identity_state": "verified" if public_id else "unknown",
        "reply_count": reply_count,
        "_present_fields": tuple(sorted(present)),
        "_source": "preview" if preview else "full",
    }


def normalize_liked_video(raw: dict) -> dict:
    video_id = _string(raw.get("aweme_id"))
    if not re.fullmatch(r"[0-9]{1,32}", video_id):
        raise PageFailure("视频列表中存在无效视频 ID，本页未确认完成。")
    author = raw.get("author")
    author = author if isinstance(author, dict) else {}
    uid, sec_uid = (_string(author.get(key)).strip() for key in ("uid", "sec_uid"))
    uid, sec_uid = ("" if value == "0" else value for value in (uid, sec_uid))
    number = _string(author.get("unique_id")).strip()
    number = "" if number == "0" else number
    try:
        stamp = float(raw.get("create_time"))
        stamp = int(stamp) if math.isfinite(stamp) and 0 < stamp < 253402300800 else None
    except (TypeError, ValueError, OverflowError):
        stamp = None
    return {
        "video_id": video_id, "title": _string(raw.get("desc")),
        "author_name": _string(author.get("nickname")), "author_douyin_id": number,
        "created_at": stamp, "video_url": f"https://www.douyin.com/video/{video_id}",
        "uid": uid, "sec_uid": sec_uid, "author_key": uid or sec_uid or f"video:{video_id}",
    }
