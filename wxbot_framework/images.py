"""本地图片文件校验常量；不连接网络或原生微信服务。"""
from __future__ import annotations

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def has_image_signature(header):
    """按文件头识别图片；不使用扩展名，也不执行完整解码。"""
    return (header.startswith(b"\xff\xd8\xff") or
            header.startswith(b"\x89PNG\r\n\x1a\n") or
            header.startswith((b"GIF87a", b"GIF89a")) or
            (header.startswith(b"RIFF") and header[8:12] == b"WEBP") or
            header.startswith(b"BM"))
