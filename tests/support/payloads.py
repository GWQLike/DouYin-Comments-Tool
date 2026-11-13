"""Sample responses for comment, profile, search and list requests."""


def comment(cid, *, user=None, replies=0, text="测试评论", stamp=1720000000):
    return {"cid": cid, "text": text, "create_time": stamp, "ip_label": "广东",
            "reply_comment_total": replies,
            "user": user if user is not None else {"uid": "u1", "sec_uid": "s1", "unique_id": "public.one", "nickname": "测试用户"}}


def page(rows, cursor=0, more=0):
    return {"status_code": 0, "comments": rows, "cursor": cursor, "has_more": more}


def profile(account="target.user", sec="target_sec", uid="target_uid", **kwargs):
    return {"status_code": 0, "user": {"uid": uid, "sec_uid": sec, "unique_id": account,
                                        "nickname": "目标昵称", **kwargs}}


def video(vid="1", author=None, stamp=1720000000):
    return {"aweme_id": vid, "desc": "测试视频标题\n原始文本", "create_time": stamp,
            "author": author if author is not None else {"uid": "author_uid", "sec_uid": "author_sec",
                                                          "unique_id": "author.one", "nickname": "视频作者"}}


def likes(rows, cursor=0, more=0):
    return {"status_code": 0, "aweme_list": rows, "max_cursor": cursor, "has_more": more}


def users(rows, cursor=0, more=0):
    return {"status_code": 0, "user_list": [{"user_info": user} for user in rows],
            "cursor": cursor, "has_more": more}
