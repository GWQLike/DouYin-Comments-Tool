"""Independent record expectations for the production normalization entry points."""
from copy import deepcopy

import pytest

from backend.activity import profile_visibility, recommendation_time
from backend.errors import PageFailure
from backend.normalization import normalize_comment, normalize_liked_video


def test_full_comment_keeps_text_identity_and_field_presence():
    raw = {
        'cid': 21, 'text': '謝謝🙂\r\n分享\\原文', 'create_time': '1720000000',
        'ip_label': '广东', 'reply_comment_total': 2,
        'user': {'uid': 31, 'sec_uid': 's31', 'unique_id': ' public.id ',
                 'short_id': 'internal-number', 'nickname': '繁體昵称'},
    }
    before = deepcopy(raw)
    expected = {
        'comment_id': '21', 'video_id': '12', 'parent_id': '20',
        'text': '謝謝🙂\r\n分享\\原文', 'created_at': 1720000000, 'ip_label': '广东',
        'nickname': '繁體昵称', 'douyin_id': 'public.id', 'uid': '31', 'sec_uid': 's31',
        'author_key': '31', 'identity_state': 'verified', 'reply_count': 2,
        '_present_fields': ('created_at', 'douyin_id', 'ip_label', 'nickname',
                            'reply_count', 'sec_uid', 'text', 'uid'),
        '_source': 'full',
    }
    assert normalize_comment(raw, 12, 20) == expected
    assert normalize_comment(raw, 12, 20) == expected
    assert raw == before


def test_sparse_preview_does_not_claim_missing_fields():
    assert normalize_comment({'comment_id': 'reply'}, '12', '21', preview=True) == {
        'comment_id': 'reply', 'video_id': '12', 'parent_id': '21', 'text': '',
        'created_at': None, 'ip_label': '', 'nickname': '', 'douyin_id': '',
        'uid': '', 'sec_uid': '', 'author_key': 'comment:reply',
        'identity_state': 'unknown', 'reply_count': 1,
        '_present_fields': (), '_source': 'preview',
    }


@pytest.mark.parametrize('value,present', [('', True), (None, False), (123, False)])
def test_explicit_empty_text_is_different_from_unavailable_text(value, present):
    row = normalize_comment({'cid': '1', 'text': value, 'create_time': None, 'ip_label': ''}, '12')
    assert ('text' in row['_present_fields']) is present
    assert set(row['_present_fields']) - {'text'} == {'created_at', 'ip_label'}
    assert row['created_at'] is None and row['ip_label'] == ''


@pytest.mark.parametrize('fields,expected', [
    ({}, 1), ({'reply_comment_total': 0}, 0), ({'reply_comment_total': -3}, 0),
    ({'reply_comment_total': 'unknown'}, 1),
    ({'reply_comment_total': 0, 'reply_comment': [{'cid': '2'}]}, 1),
])
def test_reply_evidence_keeps_unknown_threads_queryable(fields, expected):
    row = normalize_comment({'cid': '1', **fields}, '12')
    assert row['reply_count'] == expected


@pytest.mark.parametrize('stamp,expected', [
    (None, None), ('', None), ('invalid', None), (0, None), (-1, None),
    (float('inf'), None), (float('nan'), None), (253402300800, None),
    (1, 1), ('1720000000.75', 1720000000), (253402300799, 253402300799),
])
def test_publication_time_bounds_in_both_record_formats(stamp, expected):
    assert normalize_comment({'cid': '1', 'create_time': stamp}, '12')['created_at'] == expected
    assert normalize_liked_video({'aweme_id': '12', 'create_time': stamp})['created_at'] == expected


def test_video_record_keeps_original_title_and_does_not_invent_action_time():
    raw = {'aweme_id': 12, 'desc': '標題🙂\n第二行', 'create_time': 1720000000,
           'favorite_time': 1791267788, 'recommend_time': 1791267788,
           'author': {'uid': ' 31 ', 'sec_uid': ' s31 ', 'unique_id': ' public.id ',
                      'nickname': '作者'}}
    before = deepcopy(raw)
    assert normalize_liked_video(raw) == {
        'video_id': '12', 'title': '標題🙂\n第二行', 'author_name': '作者',
        'author_douyin_id': 'public.id', 'created_at': 1720000000,
        'video_url': 'https://www.douyin.com/video/12', 'uid': '31', 'sec_uid': 's31',
        'author_key': '31',
    }
    assert raw == before


@pytest.mark.parametrize('user,key', [
    (None, ''), ([], ''), ({'uid': '0', 'sec_uid': '0'}, ''),
    ({'uid': '31', 'short_id': 'legacy-number'}, '31'),
    ({'sec_uid': 's31', 'unique_id': '0'}, 's31'),
])
def test_missing_public_number_is_not_replaced_by_internal_ids(user, key):
    comment = normalize_comment({'cid': '1', 'user': user}, '12')
    video = normalize_liked_video({'aweme_id': '12', 'author': user})
    assert comment['douyin_id'] == '' and comment['identity_state'] == 'unknown'
    assert video['author_douyin_id'] == ''
    assert comment['author_key'] == (key or 'comment:1')
    assert video['author_key'] == (key or 'video:12')


@pytest.mark.parametrize('value', [None, '', 'not-a-video', '1' * 33])
def test_invalid_video_identity_rejects_the_page(value):
    with pytest.raises(PageFailure, match='无效视频 ID'):
        normalize_liked_video({'aweme_id': value})


def test_missing_comment_id_rejects_the_page():
    with pytest.raises(PageFailure, match='缺少评论 ID'):
        normalize_comment({'text': '不能形成记录'}, '12')


@pytest.mark.parametrize('value,expected', [
    (True, 'public'), (False, 'private'), (0, 'unknown'),
    (1, 'unknown'), ('false', 'unknown'), (None, 'unknown'),
])
def test_only_boolean_visibility_is_permission_evidence(value, expected):
    assert profile_visibility({'show_favorite_list': value}) == expected
    assert profile_visibility({'favorite_permission': 0}) == 'unknown'


def test_recommendation_time_uses_only_consistent_target_labels():
    target = {'uid': '31', 'sec_uid': 's31'}
    labels = [
        {'user': {'uid': 'other'}, 'recommend_time': 1791267900},
        {'user': {'uid': '31', 'sec_uid': 'wrong'}, 'recommend_time': 1791267800},
        {'user': {'uid': '31', 'sec_uid': 's31'}, 'recommend_time': 1791267788},
    ]
    raw = {'friend_recommend_info': {'label_user_list': labels}}
    before = deepcopy(raw)
    assert recommendation_time(raw, target) == 1791267788
    assert raw == before
    labels.append({'user': {'uid': '31'}, 'recommend_time': 1791267788})
    assert recommendation_time(raw, target) == 1791267788
    labels.append({'user': {'uid': '31'}, 'recommend_time': 1791267789})
    assert recommendation_time(raw, target) is None
    assert recommendation_time({'create_time': 1791267788, 'recommend_time': 1791267788,
                                'aweme_date': {'date_map': {'31': 1791267788}}}, target) is None


@pytest.mark.parametrize('stamp', [True, 0, 1.5, 'invalid', float('inf'), 253402300800])
def test_invalid_target_recommendation_time_stays_unknown(stamp):
    raw = {'friend_recommend_info': {'label_user_list': [
        {'user': {'uid': '31'}, 'recommend_time': stamp}]}}
    assert recommendation_time(raw, {'uid': '31'}) is None
