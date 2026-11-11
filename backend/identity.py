"""Public account numbers and profile identity checks shared by the collectors."""
from __future__ import annotations

import unicodedata
from typing import Any

from backend.normalization import _string


def account_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", _string(value).strip()).casefold()


def public_number(user: dict) -> str:
    value = _string(user.get("unique_id")).strip()
    if value in ("", "0"):
        value = _string(user.get("short_id")).strip()
    return "" if value == "0" else value


def normalize_profile(profile: dict, author: dict) -> dict | None:
    """Only a matching profile can establish the legacy, publicly displayed short_id."""
    if not isinstance(profile, dict):
        return None
    user = profile.get("user")
    if not isinstance(user, dict):
        return None
    uid, sec_uid = _string(user.get("uid")), _string(user.get("sec_uid"))
    uid, sec_uid = ('' if value == '0' else value for value in (uid, sec_uid))
    for key, actual in (("uid", uid), ("sec_uid", sec_uid)):
        expected = _string(author.get(key))
        if expected and actual and expected != actual:
            return None
    confirmed = bool((uid and uid == author.get("uid")) or
                     (sec_uid and sec_uid == author.get("sec_uid")))
    if not confirmed:
        return None
    public_id = public_number(user)
    return {
        "uid": uid or _string(author.get("uid")),
        "sec_uid": sec_uid or _string(author.get("sec_uid")),
        "nickname": _string(user.get("nickname")) or _string(author.get("nickname")),
        "douyin_id": public_id,
        "identity_state": "verified" if public_id else "unknown",
    }
