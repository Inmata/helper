"""一轮回复的截止时间与调用额度；限时线程显式传递同一上下文。"""
from contextvars import ContextVar
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
import threading
import time

CURRENT = ContextVar("reply_budget", default=None)
_WORK = ContextVar("reply_work_deadline", default=None)


class ReplyExpired(TimeoutError):
    pass


@dataclass(frozen=True)
class ReplyDraft:
    revision: int
    text: str
    needs_review: Callable[[], bool] | None
    on_sent: Callable[[str], None] | None = None


class ReplyBudget:
    def __init__(self, arrived, seconds=90, *, clock=time.monotonic):
        self.clock = clock
        self.deadline = arrived + seconds
        self.prepare_until = self.deadline - 70
        self.answer_until = self.deadline - 10
        self.notice_at = arrived + 30
        self.calls = {}
        self.llm_limit = 3
        self.source_id = 0
        self.stage = "排队"
        self.closed = False
        self.sending = False
        self.attempted = False
        self.sent = False
        self.progress = False
        self.revision = 0
        self.reviewing_additions = False
        self.ready = None
        self.lock = threading.RLock()

    def revise(self):
        with self.lock:
            if self.closed:
                raise ReplyExpired("本轮回复已结束")
            self.revision += 1
            self.ready = None
            self.stage = "理解新条件"

    def draft_is_current(self, draft):
        with self.lock:
            return (draft.revision == self.revision and not self.reviewing_additions
                    and not (draft.needs_review is not None and draft.needs_review()))

    @contextmanager
    def work(self, stage, until):
        with self.lock:
            self.stage = stage
        token = _WORK.set((self, until))
        try:
            yield
        finally:
            _WORK.reset(token)

    def remaining(self, cap, *, kind=None, until=None):
        with self.lock:
            deadline = self.deadline if until is None else min(self.deadline, until)
            remaining = deadline - self.clock()
            if self.closed or remaining <= 0:
                raise ReplyExpired("本轮回复已到截止时间")
            if kind:
                limit = {"llm": self.llm_limit, "jev": 8, "search": 2}[kind]
                if self.calls.get(kind, 0) >= limit:
                    raise ReplyExpired("本轮调用额度已用完")
                self.calls[kind] = self.calls.get(kind, 0) + 1
                if until is None:
                    self.stage = {"llm": "组织回答", "jev": "核对上下文", "search": "检索资料"}[kind]
            return min(cap, remaining)

    @contextmanager
    def delivery(self, revision=None):
        with self.lock:
            if (self.closed or self.clock() >= self.deadline or
                    (revision is not None and revision != self.revision)):
                raise ReplyExpired("本轮回复已结束")
            self.sending = True
            self.attempted = True
        try:
            yield
        finally:
            with self.lock:
                self.sending = False

    def submitted(self):
        with self.lock:
            self.sent = True


def remaining(cap, *, kind=None):
    budget = CURRENT.get()
    if budget is None:
        return cap
    work = _WORK.get()
    return budget.remaining(cap, kind=kind, until=work[1] if work and work[0] is budget else None)
