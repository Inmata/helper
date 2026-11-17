"""一轮回复的截止时间与调用额度；限时线程显式传递同一上下文。"""
from contextvars import ContextVar
from contextlib import contextmanager
import threading
import time

CURRENT = ContextVar("reply_budget", default=None)


class ReplyExpired(TimeoutError):
    pass


class ReplyBudget:
    def __init__(self, arrived, seconds=90, *, clock=time.monotonic):
        self.clock = clock
        self.deadline = arrived + seconds
        self.notice_at = arrived + 30
        self.calls = {}
        self.stage = "排队"
        self.closed = False
        self.sending = False
        self.sent = False
        self.progress = False
        self.ready = ""
        self.lock = threading.RLock()

    def remaining(self, cap, *, kind=None):
        with self.lock:
            remaining = self.deadline - self.clock()
            if self.closed or remaining <= 0:
                raise ReplyExpired("本轮回复已到截止时间")
            if kind:
                limit = {"llm": 3, "jev": 8, "search": 2}[kind]
                if self.calls.get(kind, 0) >= limit:
                    raise ReplyExpired("本轮调用额度已用完")
                self.calls[kind] = self.calls.get(kind, 0) + 1
                self.stage = {"llm": "组织回答", "jev": "核对上下文", "search": "检索资料"}[kind]
            return min(cap, remaining)

    @contextmanager
    def delivery(self):
        with self.lock:
            if self.closed or self.clock() >= self.deadline:
                raise ReplyExpired("本轮回复已结束")
            self.sending = True
        try:
            yield
        finally:
            with self.lock:
                self.sent = True
                self.sending = False


def remaining(cap, *, kind=None):
    budget = CURRENT.get()
    return budget.remaining(cap, kind=kind) if budget is not None else cap
