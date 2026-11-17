"""Magpie 生图工具：提交一次、按任务 ID 查询，将图片保存到本机。"""
from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from ..images import MAX_IMAGE_BYTES, has_image_signature
from .magpie_tools import (MagpieToolsFormatError, MagpieToolsToolError, _McpEndpoint, _deadline_remaining,
                           _iter_body_chunks, _set_read_timeout, urlopen)

PENDING_KEY = "usemagpie.ai/pending"
FILES_KEY = "usemagpie.ai/files"
IMAGE_ENDPOINT = "/mcp/magpie/image"
REQUEST_SECONDS = 35
SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "image/gif": ".gif", "image/bmp": ".bmp"}


class MagpieImageRejectedError(MagpieToolsToolError):
    """生图服务明确因安全审核拒绝请求。"""


@dataclass(frozen=True)
class ImageResult:
    task_id: str = ""
    path: Path | None = None


class MagpieImages:
    def __init__(self, magpie, output_dir):
        self.magpie = magpie
        self.output_dir = Path(output_dir)
        self.endpoint = _McpEndpoint(magpie.base_url + IMAGE_ENDPOINT, timeout=REQUEST_SECONDS,
                                     max_response_bytes=32 * 1024 * 1024)

    def submit(self, prompt, *, filename, reference=None, timeout=REQUEST_SECONDS):
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise ValueError("生图描述应为 1–2000 字")
        arguments = {"prompt": prompt.strip(), "n": 1}
        if reference:
            arguments["reference_images"] = [self.magpie._image_path(str(reference))]
        return self._call("generate_image", arguments, filename, timeout)

    def result(self, task_id, *, filename, timeout=REQUEST_SECONDS):
        return self._call("generation_result", {"id": task_id}, filename, timeout)

    def _call(self, tool, arguments, filename, timeout):
        deadline = time.monotonic() + timeout
        try:
            result = self.endpoint.call_raw(tool, arguments, timeout=timeout)
        except MagpieToolsToolError as error:
            if "rejected by the safety system" in str(error).casefold():
                raise MagpieImageRejectedError("生图请求被安全审核拒绝") from error
            raise
        metadata = result.get("_meta", {})
        if not isinstance(metadata, dict) or any(not isinstance(item, dict) for item in result["content"]):
            raise MagpieToolsFormatError("Magpie 生图结果格式无效")
        pending = metadata.get(PENDING_KEY)
        if pending:
            if not isinstance(pending, str):
                raise MagpieToolsFormatError("Magpie 生图任务编号格式无效")
            return ImageResult(task_id=pending)
        for item in result["content"]:
            if item.get("type") == "image":
                try:
                    data = base64.b64decode(item["data"], validate=True)
                except (KeyError, TypeError, ValueError, binascii.Error):
                    raise MagpieToolsFormatError("Magpie 生图数据无效") from None
                return self._save(data, item.get("mimeType"), filename)
        files = metadata.get(FILES_KEY, [])
        if files:
            if not isinstance(files, list) or not isinstance(files[0], dict) or not isinstance(files[0].get("url"), str):
                raise MagpieToolsFormatError("Magpie 生图下载信息无效")
            return self._download(files[0]["url"], filename, deadline)
        raise MagpieToolsFormatError("Magpie 没有返回图片或待完成任务")

    def _download(self, url, filename, deadline):
        target, origin = urlsplit(url), urlsplit(self.magpie.base_url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or not target.path.startswith(IMAGE_ENDPOINT + "/files/"):
            raise MagpieToolsFormatError("图片下载地址不属于当前 Magpie 服务")
        with urlopen(Request(url), timeout=_deadline_remaining(deadline)) as response:
            data = bytearray()
            stream = _iter_body_chunks(response, 64 * 1024)
            try:
                while True:
                    _set_read_timeout(response, _deadline_remaining(deadline))
                    chunk = next(stream, b"")
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise MagpieToolsFormatError("生成图片超过 20 MiB")
            finally:
                stream.close()
            mime = response.headers.get("Content-Type", "").split(";", 1)[0]
        return self._save(data, mime, filename)

    def _save(self, data, mime, filename):
        if mime not in SUFFIXES or not has_image_signature(data[:16]) or len(data) > MAX_IMAGE_BYTES:
            raise MagpieToolsFormatError("生成图片格式或大小不符合发送要求")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / (filename + SUFFIXES[mime])
        path.write_bytes(data)
        return ImageResult(path=path.resolve())
