"""Magpie 的 Streamable HTTP MCP 工具适配：看图、联网搜索与网页抓取。

仓库依赖里没有 MCP SDK（见 ``requirements.txt``），这里只用标准库实现 2025-03-26
Streamable HTTP 的最小客户端：POST JSON-RPC，响应可以是 ``application/json`` 或
``text/event-stream``，单次调用有总时限、响应有大小上限、返回文本有长度上限。

本机验证（2026-10-29，127.0.0.1:3425）：

* ``/mcp/magpie/vision``：``analyze_image``，参数 ``image``（绝对路径/URL/data URL）、
  ``prompt``（可选）。
* ``/mcp/magpie/web-search``：``web_search``（``query``）、``web_fetch``（``url``，
  可选 ``max_chars``、``offset``）。
* 两个端点都是无状态服务，不返回 ``Mcp-Session-Id``；客户端仍兼容有会话的服务。

安全约定：``analyze_image`` 只接受调用方给出的本机绝对路径，不接受模型生成的
URL 或 data URL；路径可再用 ``image_roots`` 限定在信任目录内。本模块不记录图片内容、
聊天正文或密钥。
"""
from __future__ import annotations

import codecs
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .deadline_http import build_deadline_opener

DEFAULT_BASE_URL = "http://127.0.0.1:3425"
VISION_ENDPOINT = "/mcp/magpie/vision"
WEB_SEARCH_ENDPOINT = "/mcp/magpie/web-search"
PROTOCOL_VERSION = "2025-03-26"

DEFAULT_TIMEOUT = 30
DEFAULT_RESULT_CHARS = 4000
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROMPT_CHARS = 2000
MAX_QUERY_CHARS = 1000
MAX_URL_CHARS = 2048
MAX_OFFSET = 10_000_000
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
CHUNK_SIZE = 64 * 1024


class MagpieToolsError(RuntimeError):
    pass


class MagpieToolsUnavailableError(MagpieToolsError):
    """连接失败、超时、端点不存在或服务端错误：可以稍后重试。"""


class MagpieToolsAuthError(MagpieToolsError):
    """密钥或访问权限问题：重试没有意义。"""


class MagpieToolsFormatError(MagpieToolsError):
    """响应不符合 MCP/JSON-RPC 约定。"""


class MagpieToolsToolError(MagpieToolsError):
    """工具本身执行失败（``isError``），例如图片读不出来。"""


@dataclass(frozen=True)
class MagpieResult:
    """工具返回的文本；``truncated`` 表示已按 ``max_result_chars`` 截断。"""

    text: str
    truncated: bool = False


class _SessionExpired(Exception):
    """服务端返回 404 且此前有会话：按 Streamable HTTP 约定重新初始化一次。"""


_opener = build_deadline_opener()


def urlopen(request, timeout):
    """模块内的 ``urlopen``：整次请求受 timeout 硬截止（含响应头阶段）、不跟随重定向；便于测试替换。"""
    return _opener.open(request, timeout=timeout)


def _limit(value, name, lower, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} 应为 {lower}–{upper} 的数值")
    return float(value)


def _int_limit(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} 应为 {lower}–{upper} 的整数")
    return value


def _clean_text(text, limit):
    """去掉首尾空白并截到 ``limit``；返回文本与是否被截断。"""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _jsonrpc_message(error):
    message = " ".join(str(error.get("message", "未知错误")).split())
    return message[:200] or "未知错误"


def _sse_event(block):
    """解析单个 SSE 事件块；只有注释或心跳（没有 ``data:``）时返回 ``None``。"""
    data = []
    for line in block.splitlines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
    if not data:
        return None
    try:
        return json.loads("\n".join(data))
    except ValueError:
        raise MagpieToolsFormatError("Magpie MCP 返回了无法解析的 SSE 事件") from None


def _split_sse_events(text):
    """切出已经完结（有空行分隔）的 SSE 事件，返回 (消息列表, 尚未完结的尾部)。"""
    events = []
    position = 0
    for match in re.finditer(r"\r?\n\r?\n", text):
        event = _sse_event(text[position:match.start()])
        position = match.end()
        if event is not None:
            events.append(event)
    return events, text[position:]


def _parse_events(raw, content_type):
    """解析 JSON 或 SSE 响应体；只保留 JSON-RPC 消息，忽略注释和心跳。"""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or "").lower():
        events, rest = _split_sse_events(text)
        if rest.strip():
            # 服务端没有用空行收尾：整段剩余内容按一个事件处理。
            event = _sse_event(rest)
            if event is not None:
                events.append(event)
        return events
    if not text.strip():
        return []
    try:
        document = json.loads(text)
    except ValueError:
        raise MagpieToolsFormatError("Magpie MCP 响应不是合法 JSON") from None
    return document if isinstance(document, list) else [document]


def _complete_json(raw):
    """非 SSE 响应能整段解析成 JSON 时说明已读全，不必等到连接关闭。"""
    if raw.rstrip()[-1:] not in (b"}", b"]"):
        return False
    try:
        json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return False
    return True


def _deadline_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MagpieToolsUnavailableError("Magpie MCP 调用超时")
    return remaining


def _set_read_timeout(response, seconds):
    """把已建立连接的套接字读超时收窄到本次剩余预算。

    ``urlopen`` 的 timeout 只约束单次 socket 操作：服务端持续小口发送时每次 recv 都会
    重置它，慢速流可以远超总时限。因此每读一块前按总截止时间重新设置；拿不到底层套接字
    的对象（例如测试替身）退化为只按块检查截止时间。
    """
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is None:
        return
    try:
        sock.settimeout(max(seconds, 0.001))
    except (OSError, ValueError):
        return


def _iter_body_chunks(response, size):
    """按块读取响应体。

    有 ``read1`` 时每次只做一次底层读取，避免为凑满整块而无限等待慢速服务端；
    只有 ``read()`` 的对象按 read 语义处理：返回不足 ``size`` 即视为读到结尾。
    """
    read1 = getattr(response, "read1", None)
    if read1 is None:
        while True:
            chunk = response.read(size)
            if not chunk:
                return
            yield chunk
            if len(chunk) < size:
                return
    while True:
        chunk = read1(size)
        if not chunk:
            return
        yield chunk


def _result_for(events, request_id):
    for event in events:
        if not isinstance(event, dict) or event.get("id") != request_id:
            continue
        error = event.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise MagpieToolsFormatError("Magpie MCP 返回了无效的错误对象")
            raise MagpieToolsFormatError(f"Magpie MCP 返回错误：{_jsonrpc_message(error)}")
        if "result" not in event:
            raise MagpieToolsFormatError("Magpie MCP 响应缺少 result")
        return event["result"]
    raise MagpieToolsFormatError("Magpie MCP 响应缺少对应请求的结果")


def _tool_text(result):
    """把 ``tools/call`` 的 content 数组拼成文本；``isError`` 抛可判别的工具错误。"""
    if not isinstance(result, dict):
        raise MagpieToolsFormatError("Magpie MCP 工具结果不是对象")
    content = result.get("content")
    if not isinstance(content, list):
        raise MagpieToolsFormatError("Magpie MCP 工具结果缺少 content")
    parts = [item["text"] for item in content
             if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
    text = "\n".join(parts)
    if result.get("isError") is True:
        raise MagpieToolsToolError(_clean_text(text, 500)[0] or "Magpie 工具执行失败")
    if not text.strip():
        raise MagpieToolsFormatError("Magpie MCP 工具没有返回文本")
    return text


class _McpEndpoint:
    """单个 MCP 端点的 JSON-RPC 会话；首次调用时初始化，之后复用会话。"""

    def __init__(self, url, *, timeout, max_response_bytes=MAX_RESPONSE_BYTES):
        self.url = url
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._session = None
        self._initialized = False
        self._lock = threading.Lock()
        self._ids = count(1)

    def _post(self, payload, deadline, expect_id=None):
        """发送一条 JSON-RPC 消息并返回响应事件；``expect_id`` 为空表示通知，不读响应体。"""
        remaining = _deadline_remaining(deadline)
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        session = self._session
        if session:
            headers["Mcp-Session-Id"] = session
        request = Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, method="POST")
        try:
            with urlopen(request, timeout=remaining) as response:
                content_type = response.headers.get("Content-Type", "")
                assigned = response.headers.get("Mcp-Session-Id")
                # 通知没有响应体，也不应为服务端保持的流等待到截止时间。
                events = ([] if expect_id is None
                          else self._read_body(response, deadline, content_type, expect_id))
        except HTTPError as error:
            if error.code in (401, 403):
                raise MagpieToolsAuthError(f"Magpie MCP 端点 HTTP {error.code}；请核对访问配置") from None
            if error.code == 404 and self._session:
                raise _SessionExpired from None
            raise MagpieToolsUnavailableError(f"Magpie MCP 端点 HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise MagpieToolsUnavailableError("Magpie MCP 连接失败或超时") from None
        if assigned and not self._session:
            self._session = assigned
        return events

    def _read_body(self, response, deadline, content_type, expect_id):
        """在总截止时间内分块读取响应体；SSE 读到对应请求的消息就提前停止。"""
        is_sse = "text/event-stream" in (content_type or "").lower()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        raw = bytearray()
        events = []
        pending = ""
        stream = _iter_body_chunks(response, CHUNK_SIZE)
        try:
            while True:
                _set_read_timeout(response, _deadline_remaining(deadline))
                chunk = next(stream, b"")
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > self.max_response_bytes:
                    raise MagpieToolsFormatError("Magpie MCP 响应过大")
                if not is_sse:
                    if _complete_json(raw):
                        break
                    continue
                pending += decoder.decode(chunk)
                found, pending = _split_sse_events(pending)
                events.extend(found)
                if any(isinstance(event, dict) and event.get("id") == expect_id for event in events):
                    return events
        finally:
            stream.close()
        if not is_sse:
            return _parse_events(bytes(raw), content_type)
        pending += decoder.decode(b"", True)
        if pending.strip():
            event = _sse_event(pending)
            if event is not None:
                events.append(event)
        return events

    def _initialize(self, deadline):
        # 等待其他线程完成初始化也必须计入总预算，锁不能无限阻塞。
        if not self._lock.acquire(timeout=_deadline_remaining(deadline)):
            raise MagpieToolsUnavailableError("Magpie MCP 初始化等待超时")
        try:
            if self._initialized:
                return
            self._session = None
            request_id = next(self._ids)
            events = self._post({"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "helper-magpie-tools", "version": "1"}}}, deadline, request_id)
            result = _result_for(events, request_id)
            if not isinstance(result, dict):
                raise MagpieToolsFormatError("Magpie MCP 初始化结果不是对象")
            # 通知没有响应体；部分服务端也不严格要求，失败不影响后续调用。
            try:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, deadline)
            except (MagpieToolsError, _SessionExpired):
                pass
            self._initialized = True
        finally:
            self._lock.release()

    def _reset(self, deadline):
        if not self._lock.acquire(timeout=_deadline_remaining(deadline)):
            raise MagpieToolsUnavailableError("Magpie MCP 会话重置等待超时")
        try:
            self._initialized = False
            self._session = None
        finally:
            self._lock.release()

    def call(self, name, arguments, *, timeout=None):
        """调用一个工具并返回文本；同一请求最多重试一次（仅会话过期时）。

        ``timeout`` 是调用方剩余的时间，只能缩短、不能超过配置的总超时。
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else min(self.timeout, timeout))
        self._initialize(deadline)
        request = {"jsonrpc": "2.0", "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        for attempt in (0, 1):
            request["id"] = next(self._ids)
            try:
                events = self._post(request, deadline, request["id"])
            except _SessionExpired:
                if attempt:
                    raise MagpieToolsUnavailableError("Magpie MCP 会话失效且重新初始化后仍被拒绝") from None
                self._reset(deadline)
                self._initialize(deadline)
                continue
            return _tool_text(_result_for(events, request["id"]))
        raise MagpieToolsUnavailableError("Magpie MCP 会话失效")  # pragma: no cover - 循环内必返回


class MagpieTools:
    """看图与联网工具客户端；只供可信调用方使用，不把模型给的路径直接转发。"""

    def __init__(self, options=None):
        options = {} if options is None else options
        if not isinstance(options, dict):
            raise ValueError("Magpie 工具配置必须是 JSON 对象")
        base_url = options.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("Magpie base_url 不能为空")
        base_url = base_url.rstrip("/")
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Magpie base_url 应为不含认证信息的 HTTP(S) 地址")
        if parsed.scheme == "http" and (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("非本机地址的 Magpie base_url 必须使用 HTTPS")
        self.base_url = base_url
        self.timeout = _limit(options.get("timeout_seconds", DEFAULT_TIMEOUT), "timeout_seconds", 1, 120)
        self.max_result_chars = _int_limit(options.get("max_result_chars", DEFAULT_RESULT_CHARS),
                                           "max_result_chars", 200, 20000)
        self.max_image_bytes = _int_limit(options.get("max_image_bytes", DEFAULT_MAX_IMAGE_BYTES),
                                          "max_image_bytes", 1024, 50 * 1024 * 1024)
        roots = options.get("image_roots", [])
        if not isinstance(roots, (list, tuple)):
            raise ValueError("image_roots 应为目录列表")
        self.image_roots = []
        for root in roots:
            if not isinstance(root, str) or not root.strip() or "\0" in root or not Path(root).is_absolute():
                raise ValueError("image_roots 的每一项都应是本机绝对目录")
            self.image_roots.append(Path(root).resolve())
        self._vision = _McpEndpoint(base_url + VISION_ENDPOINT, timeout=self.timeout)
        self._web = _McpEndpoint(base_url + WEB_SEARCH_ENDPOINT, timeout=self.timeout)

    @classmethod
    def from_file(cls, path):
        """从大模型配置文件读取可选的 ``magpie_tools`` 段；缺省项用本机默认值。"""
        document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        section = document.get("magpie_tools", {})
        if not isinstance(section, dict):
            raise ValueError("magpie_tools 配置必须是 JSON 对象")
        return cls(section)

    def _result_limit(self, max_chars):
        if max_chars is None:
            return self.max_result_chars
        if type(max_chars) is not int or not 1 <= max_chars <= self.max_result_chars:
            raise ValueError(f"max_chars 应为 1–{self.max_result_chars} 的整数")
        return max_chars

    def _image_path(self, image):
        """校验可信图片路径：本机绝对路径、文件存在、格式和大小受限、可在信任目录内。"""
        if not isinstance(image, str) or not image.strip() or "\0" in image:
            raise ValueError("图片必须是非空的本机绝对路径")
        path = Path(image)
        if not path.is_absolute():
            raise ValueError("图片必须使用本机绝对路径，不接受 URL 或相对路径")
        resolved = path.resolve()
        if resolved.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError("图片格式只支持 PNG、JPEG、GIF、WebP")
        if not resolved.is_file():
            raise ValueError("图片文件不存在")
        if self.image_roots and not any(resolved == root or root in resolved.parents
                                        for root in self.image_roots):
            raise ValueError("图片不在允许的目录内")
        if resolved.stat().st_size > self.max_image_bytes:
            raise ValueError(f"图片超过 {self.max_image_bytes} 字节上限")
        return str(resolved)

    @staticmethod
    def _call_timeout(timeout):
        return None if timeout is None else _limit(timeout, "timeout", 0.1, 120)

    def analyze_image(self, image, prompt="", *, max_chars=None, timeout=None):
        """让 Magpie 用文字回答图片内容；``image`` 必须来自可信调用方。"""
        path = self._image_path(image)
        if not isinstance(prompt, str):
            raise ValueError("图片提示词必须是字符串")
        prompt = prompt.strip()
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"图片提示词不能超过 {MAX_PROMPT_CHARS} 字")
        limit = self._result_limit(max_chars)
        timeout = self._call_timeout(timeout)
        arguments = {"image": path}
        if prompt:
            arguments["prompt"] = prompt
        return self._bounded(self._vision.call("analyze_image", arguments, timeout=timeout), limit)

    def search(self, query, *, max_chars=None, timeout=None):
        """联网搜索；返回网页摘要和 URL，文本受 ``max_result_chars`` 限制。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("搜索词不能为空")
        query = query.strip()
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"搜索词不能超过 {MAX_QUERY_CHARS} 字")
        limit = self._result_limit(max_chars)
        timeout = self._call_timeout(timeout)
        return self._bounded(self._web.call("web_search", {"query": query}, timeout=timeout), limit)

    def fetch(self, url, *, max_chars=None, offset=0):
        """抓取网页正文；``max_chars`` 同时作为服务端和本地的长度上限。"""
        if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL_CHARS:
            raise ValueError("网页地址不能为空且不能超过长度上限")
        url = url.strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValueError("网页地址无效") from None
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("网页地址必须是无认证信息的 http(s) URL")
        if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
            raise ValueError(f"offset 应为 0–{MAX_OFFSET} 的整数")
        limit = self._result_limit(max_chars)
        arguments = {"url": url, "max_chars": limit, "offset": offset}
        return self._bounded(self._web.call("web_fetch", arguments), limit)

    def _bounded(self, text, max_chars):
        limit = self._result_limit(max_chars)
        text, truncated = _clean_text(text, limit)
        return MagpieResult(text, truncated)
