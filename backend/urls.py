"""Validate user-provided share text without making a network request."""
import re
from urllib.parse import parse_qs, urlparse

ALLOWED_HOSTS = {'douyin.com', 'www.douyin.com', 'v.douyin.com', 'iesdouyin.com', 'www.iesdouyin.com'}


def parse_input(value: str) -> tuple[str, str]:
    urls = re.findall(r'https?://[^\s<>\"\u3000]+', value.strip())
    if len(urls) != 1:
        raise ValueError('请粘贴一个抖音视频链接，或包含一个链接的分享文本')
    url = urls[0].rstrip('，。！？；、）)]}》')
    parsed = urlparse(url)
    if parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('仅支持抖音官方视频链接和短分享链接')
    if parsed.hostname == 'v.douyin.com':
        if not parsed.path.strip('/'):
            raise ValueError('短分享链接不完整')
        return url, ''
    match = re.search(r'/(?:share/)?video/(\d+)(?:/|$)', parsed.path)
    video_id = match.group(1) if match else parse_qs(parsed.query).get('modal_id', [''])[0]
    if not video_id.isdigit():
        raise ValueError('链接中没有找到视频 ID，请复制视频分享链接')
    return url, video_id
