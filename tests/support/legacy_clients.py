"""Fake clients for the historical collectors."""
from collections import defaultdict

from .payloads import likes, page, profile, users


class FakeClient:
    def __init__(self, main=None, replies=None, profiles=None):
        self.main = main or {0: page([])}
        self.replies = replies or {}
        self.profiles = profiles or {}
        self.calls = []
        self.attempts = defaultdict(int)
        self.manual_waits = 0

    def response(self, mapping, key):
        self.calls.append(key)
        result = mapping[key]
        if isinstance(result, list):
            count = self.attempts[key]
            result = result[min(count, len(result) - 1)]
            self.attempts[key] += 1
        if isinstance(result, Exception):
            raise result
        return result

    async def get_aweme_comments(self, video_id, cursor):
        return self.response(self.main, int(cursor))

    async def get_sub_comments(self, video_id, parent_id, cursor):
        return self.response(self.replies, (parent_id, int(cursor)))

    async def get_user_info(self, sec_uid):
        return self.response(self.profiles, sec_uid)

    async def get_video_by_id(self, video_id):
        return {"desc": "离线测试视频"}

    async def wait_for_manual(self):
        self.manual_waits += 1

class FakeLikesClient:
    def __init__(self, pages=None, search=None, target=None, profiles=None):
        target = target if target is not None else profile(show_favorite_list=True)
        self.pages = pages if pages is not None else {0: likes([])}
        self.search = search if search is not None else {0: users([target["user"]])}
        self.profiles = {"target_sec": target, **(profiles or {})}
        self.calls = []
        self.attempts = defaultdict(int)
        self.manual_waits = 0

    def response(self, mapping, key, family):
        self.calls.append((family, key))
        result = mapping[key]
        if isinstance(result, list):
            index = self.attempts[(family, key)]
            self.attempts[(family, key)] += 1
            result = result[min(index, len(result) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    async def search_users(self, account, offset=0, search_id=""):
        assert account == "target.user"
        return self.response(self.search, offset, "search")

    async def get_user_info(self, sec_uid):
        return self.response(self.profiles, sec_uid, "profile")

    async def get_user_likes(self, sec_uid, cursor=0):
        assert sec_uid == "target_sec"
        return self.response(self.pages, cursor, "likes")

    async def wait_for_manual(self):
        self.manual_waits += 1

class FakeRecommendationsClient(FakeLikesClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.counts = []

    async def get_user_likes(self, *args, **kwargs):
        raise AssertionError('Recommendations must not request likes')

    async def get_user_recommendations(self, sec_uid, cursor=0, count=18):
        assert sec_uid == 'target_sec'
        self.counts.append(count)
        return self.response(self.pages, cursor, 'recommendations')
