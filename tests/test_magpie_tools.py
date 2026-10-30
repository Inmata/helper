"""Magpie MCP 适配的离线验证：请求形状、响应解析、超时、错误与图片校验。"""
import contextlib
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from support.recyclable_tempdir import RecyclableTemporaryDirectory

from wxbot_framework.integrations.magpie_tools import (MagpieResult, MagpieTools, MagpieToolsAuthError,
                                                       MagpieToolsFormatError, MagpieToolsToolError,
                                                       MagpieToolsUnavailableError, _McpEndpoint)

BASE = "http://127.0.0.1:3425"
VISION_URL = BASE + "/mcp/magpie/vision"
SEARCH_URL = BASE + "/mcp/magpie/web-search"


class Response:
    def __init__(self, body=b"", *, status=200, content_type="application/json", session=None):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = {"Content-Type": content_type}
        if session:
            self.headers["Mcp-Session-Id"] = session

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def init_body(request_id=1, session=None):
    result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}
    return Response(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), session=session)


def call_body(text, request_id=2, *, is_error=False):
    return Response(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {
        "content": [{"type": "text", "text": text}], "isError": is_error}}))


def handshake_and_call(text, session=None):
    return [init_body(session=session), Response(status=202), call_body(text)]


class RequestShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = RecyclableTemporaryDirectory()
        cls.image = Path(cls._directory.name) / "sample.png"
        cls.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def setUp(self):
        self.tools = MagpieTools()

    def test_vision_handshake_and_arguments(self):
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("一只蓝色的猫")) as call:
            result = self.tools.analyze_image(str(self.image), "画面里是什么？")
        self.assertEqual(result, MagpieResult("一只蓝色的猫", False))
        requests = [item.args[0] for item in call.call_args_list]
        payloads = [json.loads(request.data) for request in requests]
        self.assertEqual([payload["method"] for payload in payloads],
                         ["initialize", "notifications/initialized", "tools/call"])
        self.assertEqual(requests[0].full_url, VISION_URL)
        self.assertIn("text/event-stream", requests[0].get_header("Accept"))
        self.assertEqual(payloads[2]["params"]["name"], "analyze_image")
        self.assertEqual(payloads[2]["params"]["arguments"],
                         {"image": str(self.image.resolve()), "prompt": "画面里是什么？"})
        self.assertTrue(all(item.kwargs["timeout"] <= 30 for item in call.call_args_list))

    def test_blank_prompt_is_omitted(self):
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("描述")) as call:
            self.tools.analyze_image(str(self.image), "   ")
        payload = json.loads(call.call_args_list[2].args[0].data)
        self.assertNotIn("prompt", payload["params"]["arguments"])

    def test_search_and_fetch_use_the_web_endpoint_and_bounded_lengths(self):
        tools = MagpieTools({"max_result_chars": 500})
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("搜索结果")) as call:
            self.assertEqual(tools.search("  今天的新闻  "), MagpieResult("搜索结果", False))
        self.assertEqual(call.call_args_list[0].args[0].full_url, SEARCH_URL)
        payload = json.loads(call.call_args_list[2].args[0].data)
        self.assertEqual(payload["params"]["arguments"], {"query": "今天的新闻"})

        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("正文" * 400)) as call:
            fetch_tools = MagpieTools({"max_result_chars": 500})
            result = fetch_tools.fetch("https://example.com/page", max_chars=300, offset=10)
        payload = json.loads(call.call_args_list[2].args[0].data)
        self.assertEqual(payload["params"]["arguments"],
                         {"url": "https://example.com/page", "max_chars": 300, "offset": 10})
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.text), 300)

    def test_search_result_is_truncated_to_the_configured_limit(self):
        tools = MagpieTools({"max_result_chars": 200})
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("字" * 500)):
            result = tools.search("查询")
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.text), 200)

    def test_sse_responses_are_parsed(self):
        event = ('event: message\r\ndata: {"jsonrpc":"2.0","id":2,"result":{"content":'
                 '[{"type":"text","text":"来自 SSE"}],"isError":false}}\r\n\r\n')
        with patch("wxbot_framework.integrations.magpie_tools.urlopen", side_effect=[
                init_body(), Response(status=202),
                Response(event, content_type="text/event-stream")]):
            self.assertEqual(self.tools.search("查询"), MagpieResult("来自 SSE", False))

    def test_session_header_is_reused(self):
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("结果", session="session-1")) as call:
            self.tools.search("查询")
        third = call.call_args_list[2].args[0]
        self.assertEqual(third.get_header("Mcp-session-id"), "session-1")

    def test_expired_session_reinitializes_once(self):
        expired = HTTPError(VISION_URL, 404, "gone", {}, None)
        with patch("wxbot_framework.integrations.magpie_tools.urlopen", side_effect=[
                init_body(session="old"), Response(status=202), expired,
                init_body(request_id=3, session="new"), Response(status=202),
                call_body("重试成功", request_id=4)]) as call:
            result = self.tools.analyze_image(str(self.image), "看看")
        self.assertEqual(result.text, "重试成功")
        self.assertEqual(call.call_count, 6)


class FailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = RecyclableTemporaryDirectory()
        cls.image = Path(cls._directory.name) / "sample.png"
        cls.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def setUp(self):
        self.tools = MagpieTools()

    def test_tool_isError_is_distinguishable(self):
        with patch("wxbot_framework.integrations.magpie_tools.urlopen", side_effect=[
                init_body(), Response(status=202), call_body("图片读不出来", is_error=True)]):
            with self.assertRaises(MagpieToolsToolError) as caught:
                self.tools.analyze_image(str(self.image))
        self.assertIn("图片读不出来", str(caught.exception))

    def test_http_and_connection_errors_are_distinguishable(self):
        cases = (
            (HTTPError(VISION_URL, 401, "dummy-key", {}, None), MagpieToolsAuthError),
            (HTTPError(VISION_URL, 403, "dummy-key", {}, None), MagpieToolsAuthError),
            (HTTPError(VISION_URL, 500, "dummy-key", {}, None), MagpieToolsUnavailableError),
            (URLError("dummy-key"), MagpieToolsUnavailableError),
            (TimeoutError("dummy-key"), MagpieToolsUnavailableError),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), \
                    patch("wxbot_framework.integrations.magpie_tools.urlopen", side_effect=error):
                with self.assertRaises(expected) as caught:
                    self.tools.search("查询")
                self.assertNotIn("dummy-key", str(caught.exception))

    def test_malformed_responses(self):
        for body in (Response("不是 JSON"), Response(json.dumps({"jsonrpc": "2.0", "id": 1}))):
            with self.subTest(body=body._body), \
                    patch("wxbot_framework.integrations.magpie_tools.urlopen", return_value=body):
                with self.assertRaises(MagpieToolsFormatError):
                    MagpieTools().search("查询")
        jsonrpc_error = Response(json.dumps({"jsonrpc": "2.0", "id": 1,
                                             "error": {"code": -32602, "message": "bad params"}}))
        with patch("wxbot_framework.integrations.magpie_tools.urlopen", return_value=jsonrpc_error):
            with self.assertRaisesRegex(MagpieToolsFormatError, "bad params"):
                MagpieTools().search("查询")
        with patch("wxbot_framework.integrations.magpie_tools.urlopen", side_effect=[
                init_body(), Response(status=202),
                Response(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"content": []}}))]):
            with self.assertRaisesRegex(MagpieToolsFormatError, "没有返回文本"):
                MagpieTools().search("查询")

    def test_oversized_response_is_rejected(self):
        endpoint = _McpEndpoint(VISION_URL, timeout=5, max_response_bytes=16)
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   return_value=Response("x" * 100)):
            with self.assertRaisesRegex(MagpieToolsFormatError, "响应过大"):
                endpoint.call("analyze_image", {"image": "x"})

    def test_timeout_is_a_total_budget(self):
        endpoint = _McpEndpoint(VISION_URL, timeout=5)
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=TimeoutError("slow")) as call:
            with self.assertRaises(MagpieToolsUnavailableError):
                endpoint.call("analyze_image", {"image": "x"})
        self.assertLessEqual(call.call_args.kwargs["timeout"], 5)


class _ScriptedHandler(BaseHTTPRequestHandler):
    """本地 MCP 替身：按脚本慢速分块发送 SSE，用来验证真实 socket 上的总时限。"""

    protocol_version = "HTTP/1.0"
    script = staticmethod(lambda request_id: ())

    def log_message(self, *args):  # 测试输出保持干净
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        request_id = payload.get("id")
        if payload.get("method") == "initialize":
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "serverInfo": {"name": "slow", "version": "1"}}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload.get("method") != "tools/call":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for part, delay in self.script(request_id):
                time.sleep(delay)
                self.wfile.write(part.encode("utf-8"))
                self.wfile.flush()
        except OSError:  # 客户端按截止时间提前断开
            pass


@contextlib.contextmanager
def slow_mcp_server(script):
    class Handler(_ScriptedHandler):
        pass

    Handler.script = staticmethod(script)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SlowStreamTests(unittest.TestCase):
    """真实本地慢速分块 HTTP：socket 超时不能代替总截止时间。"""

    def test_slow_stream_stops_at_the_total_deadline(self):
        def script(request_id):
            return [(": keep-alive\n\n", 0.4)] * 6

        with slow_mcp_server(script) as base_url:
            tools = MagpieTools({"base_url": base_url, "timeout_seconds": 1})
            started = time.monotonic()
            with self.assertRaises(MagpieToolsUnavailableError):
                tools.search("查询")
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.8)

    def test_slow_sse_returns_as_soon_as_the_result_arrives(self):
        def script(request_id):
            result = {"jsonrpc": "2.0", "id": request_id,
                      "result": {"content": [{"type": "text", "text": "慢速结果"}], "isError": False}}
            return [(": keep-alive\n\n", 0.3),
                    ("data: %s\n\n" % json.dumps(result, ensure_ascii=False), 0.3),
                    ("", 1.0)]

        with slow_mcp_server(script) as base_url:
            tools = MagpieTools({"base_url": base_url, "timeout_seconds": 5})
            started = time.monotonic()
            result = tools.search("查询")
            elapsed = time.monotonic() - started
        self.assertEqual(result, MagpieResult("慢速结果", False))
        self.assertLess(elapsed, 1.5)


class RedirectTests(unittest.TestCase):
    """重定向必须被拒绝：每跳都会重置 socket 超时，总截止时间会失效。"""

    def test_redirects_are_not_followed(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = _McpEndpoint("http://127.0.0.1:%d/mcp/magpie/vision"
                                    % server.server_address[1], timeout=5)
            with self.assertRaises(MagpieToolsUnavailableError):
                endpoint.call("analyze_image", {"image": "x"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(paths, ["/mcp/magpie/vision"])


class InitializationLockTests(unittest.TestCase):
    def test_initialization_wait_is_bounded_by_remaining_budget(self):
        endpoint = _McpEndpoint(VISION_URL, timeout=0.25)
        with patch("wxbot_framework.integrations.magpie_tools.urlopen") as call:
            endpoint._lock.acquire()
            try:
                started = time.monotonic()
                with self.assertRaises(MagpieToolsUnavailableError):
                    endpoint.call("analyze_image", {"image": "x"})
                elapsed = time.monotonic() - started
            finally:
                endpoint._lock.release()
        call.assert_not_called()
        self.assertLess(elapsed, 1.0)


class ImageValidationTests(unittest.TestCase):
    def setUp(self):
        self.tools = MagpieTools()
        self.directory = RecyclableTemporaryDirectory()
        self.root = Path(self.directory.name)
        self.image = self.root / "photo.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    def tearDown(self):
        self.directory.cleanup()

    def test_rejects_untrusted_or_missing_images(self):
        (self.root / "note.txt").write_text("x", encoding="utf-8")
        cases = ("photo.png", str(self.root / "missing.png"), str(self.root / "note.txt"),
                 "https://example.com/photo.png", "data:image/png;base64,AAAA")
        for image in cases:
            with self.subTest(image=image), self.assertRaises(ValueError):
                self.tools.analyze_image(image)
        for image in (None, 1, ""):
            with self.subTest(image=image), self.assertRaises(ValueError):
                self.tools.analyze_image(image)

    def test_rejects_oversized_images(self):
        tools = MagpieTools({"max_image_bytes": 1024})
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 2048)
        with self.assertRaisesRegex(ValueError, "上限"):
            tools.analyze_image(str(self.image))

    def test_image_roots_confine_paths(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        inside = allowed / "inside.png"
        inside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        tools = MagpieTools({"image_roots": [str(allowed)]})
        with self.assertRaisesRegex(ValueError, "不在允许的目录"):
            tools.analyze_image(str(self.image))
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("图")):
            self.assertEqual(tools.analyze_image(str(inside)).text, "图")

    def test_prompt_query_url_and_limit_validation(self):
        with patch("wxbot_framework.integrations.magpie_tools.urlopen",
                   side_effect=handshake_and_call("图")):
            for prompt in (None, 1, "x" * 2001):
                with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                    self.tools.analyze_image(str(self.image), prompt)
        for query in ("", "   ", None, 1, "x" * 1001):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.tools.search(query)
        for url in ("ftp://example.com", "https://user:pass@example.com", "", "https://example.com/" + "x" * 3000):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.tools.fetch(url)
        for offset in (-1, 1.5, True):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.tools.fetch("https://example.com", offset=offset)
        with patch("wxbot_framework.integrations.magpie_tools.urlopen") as call:
            for value in (0, 4001, True):
                with self.subTest(tool="search", max_chars=value), self.assertRaises(ValueError):
                    self.tools.search("查询", max_chars=value)
                with self.subTest(tool="vision", max_chars=value), self.assertRaises(ValueError):
                    self.tools.analyze_image(str(self.image), max_chars=value)
            call.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_invalid_configuration(self):
        for change in ({"base_url": ""}, {"base_url": "http://example.com"},
                       {"base_url": "https://user:pass@example.com"},
                       {"timeout_seconds": 0}, {"timeout_seconds": 121}, {"timeout_seconds": True},
                       {"timeout_seconds": float("nan")},
                       {"max_result_chars": 199}, {"max_result_chars": 20001}, {"max_result_chars": True},
                       {"max_image_bytes": 100}, {"image_roots": "C:/images"},
                       {"image_roots": ["relative/dir"]}, {"image_roots": [1]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                MagpieTools(change)

    def test_from_file_reads_the_optional_section_with_bom(self):
        with RecyclableTemporaryDirectory() as directory:
            path = Path(directory) / "llm.json"
            path.write_text(json.dumps({"magpie_tools": {"timeout_seconds": 12,
                                                         "max_result_chars": 800}}), encoding="utf-8-sig")
            tools = MagpieTools.from_file(path)
        self.assertEqual((tools.timeout, tools.max_result_chars), (12, 800))
        with RecyclableTemporaryDirectory() as directory:
            path = Path(directory) / "llm.json"
            path.write_text("{}", encoding="utf-8")
            tools = MagpieTools.from_file(path)
        self.assertEqual((tools.base_url, tools.max_result_chars), (BASE, 4000))

    def test_section_must_be_an_object(self):
        with RecyclableTemporaryDirectory() as directory:
            path = Path(directory) / "llm.json"
            path.write_text(json.dumps({"magpie_tools": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                MagpieTools.from_file(path)


if __name__ == "__main__":
    unittest.main()
