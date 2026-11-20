"""从表情消息的微信 CDN 引用读取图片，原始地址只用于下载。"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .images import MAX_IMAGE_BYTES, has_image_signature


_CDN_HOSTS = {"emoji.qpic.cn", "vweixinf.tc.qq.com", "vweixin.tc.qq.com", "wxapp.tc.qq.com"}


class StickerUnavailable(ValueError):
    """没有受支持的原图引用或格式，原因可交给插件。"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_sticker(metadata, destination, is_current):
    url = metadata.sticker_url
    if not url or not metadata.md5:
        raise StickerUnavailable("表情没有可用的原图地址或校验信息")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or parts.hostname not in _CDN_HOSTS or
                parts.port not in (None, 443 if parts.scheme == "https" else 80) or
                parts.username is not None or parts.password is not None or parts.fragment or
                not parts.path.startswith("/") or "\\" in url or any(char.isspace() for char in url)):
            raise ValueError()
    except ValueError:
        raise StickerUnavailable("表情原图地址不受支持") from None

    data = bytearray()
    deadline = time.monotonic() + 8
    request = Request(url, headers={"Accept": "image/*, application/octet-stream"})
    with build_opener(_NoRedirect()).open(request, timeout=4) as response:
        while is_current() and time.monotonic() < deadline:
            chunk = response.read1(min(65_536, MAX_IMAGE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("表情原图超过大小上限")
        else:
            raise TimeoutError("表情下载已取消或超时")
    if (not data or (metadata.length and len(data) != metadata.length) or
            hashlib.md5(data).hexdigest() != metadata.md5):
        raise ValueError("表情原图校验失败")
    if not has_image_signature(data) or data.startswith(b"BM"):
        raise StickerUnavailable("表情原图格式暂不支持")
    with destination.open("xb") as target:
        target.write(data)
