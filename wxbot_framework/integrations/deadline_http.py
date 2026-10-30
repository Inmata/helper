"""带硬截止时间的 urllib opener：Jev 与 Magpie 共用。

``urlopen`` 的 timeout 只约束单次 socket 操作，服务端每次小口发送都会重新计时，
慢速发送响应头或正文时一次请求可以远超时限，调用线程和它持有的锁也就一直放不掉。
这里在 TCP 套接字一建立就启动看门狗，到截止时间关闭连接，代理 CONNECT、TLS 握手、
响应头和正文里阻塞中的读取都随即以 ``OSError`` 结束。截止时间从创建连接对象时起算，
等于本次请求的 timeout。另外不跟随重定向：每一跳都会重新计时。
"""
from __future__ import annotations

import http.client
import socket
import threading
import time
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener


def _watch(sock, deadline):
    """到截止时间关闭 ``sock`` 所在的连接。

    盯的是复制出的套接字：shutdown 作用于连接本身，所有副本都会读到结束；TLS 包装会
    detach 原套接字对象、urllib 读到响应头后也会关掉连接对象的 sock，副本都不受影响。
    副本在截止时间到达时关闭，正常读完的请求此时 shutdown 也只是空操作。
    """
    guard = sock.dup()

    def expire():
        try:
            guard.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            guard.close()

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()


class _DeadlineConnection:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        timeout = self.timeout
        if type(timeout) not in (int, float):
            return
        deadline = time.monotonic() + timeout
        create = self._create_connection

        def create_watched(*create_args, **create_kwargs):
            sock = create(*create_args, **create_kwargs)
            _watch(sock, deadline)
            return sock

        self._create_connection = create_watched


class _HTTPConnection(_DeadlineConnection, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_DeadlineConnection, http.client.HTTPSConnection):
    pass


class _HTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def build_deadline_opener(proxies=None):
    """``proxies`` 缺省按环境变量取代理（与 urllib 默认一致）；测试可显式指定。"""
    handlers = [_NoRedirectHandler, _HTTPHandler, _HTTPSHandler]
    if proxies is not None:
        handlers.append(ProxyHandler(proxies))
    return build_opener(*handlers)
