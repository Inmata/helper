"""硬截止的 HTTP opener：服务端慢速小口发送响应头时，Jev 与 Magpie 请求也在时限内结束、释放锁。"""
import socket
import threading
import time
import unittest
from urllib.request import Request

from wxbot_framework.integrations.deadline_http import build_deadline_opener
from wxbot_framework.integrations.jev import JevClient, JevError
from wxbot_framework.integrations.magpie_tools import MagpieToolsUnavailableError, _McpEndpoint


class TricklingServer:
    """本机替身：读到请求后每 30ms 发一行响应头，持续数秒，永不结束响应头。"""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        self.listener.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                continue
            threading.Thread(target=self.trickle, args=(conn,), daemon=True).start()

    def trickle(self, conn):
        with conn:
            try:
                conn.settimeout(1)
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
                for index in range(150):
                    if self.stop.is_set():
                        return
                    conn.sendall(f"X-Slow-{index}: 1\r\n".encode())
                    time.sleep(0.03)
            except OSError:
                return

    def close(self):
        self.stop.set()
        self.listener.close()


class DeadlineOpenerTests(unittest.TestCase):
    def setUp(self):
        self.server = TricklingServer()
        self.addCleanup(self.server.close)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def test_jev_slow_headers_end_at_the_deadline(self):
        client = JevClient({"base_url": self.base + "/v1", "api_key": "k", "timeout_seconds": 5})
        started = time.monotonic()
        with self.assertRaises(JevError):
            client.noul({}, [{"instructions": "问", "true": "是", "false": "否"}], timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_slow_proxy_connect_response_ends_at_the_deadline(self):
        # HTTPS 走代理时，CONNECT 的响应头在 connect() 里读；替身代理同样小口慢发。
        opener = build_deadline_opener({"https": self.base})
        started = time.monotonic()
        with self.assertRaises(OSError):
            opener.open(Request("https://jev.example.invalid/v1/systemone", data=b"{}", method="POST"),
                        timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_magpie_slow_initialize_releases_the_endpoint_lock(self):
        endpoint = _McpEndpoint(self.base + "/vision/mcp", timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(MagpieToolsUnavailableError):
            endpoint.call("analyze_image", {"image": "x"})
        self.assertLess(time.monotonic() - started, 1.5)
        # 卡住的初始化已经结束，下一轮不会被旧调用占着的锁拖住。
        self.assertTrue(endpoint._lock.acquire(blocking=False))
        endpoint._lock.release()


if __name__ == "__main__":
    unittest.main()
