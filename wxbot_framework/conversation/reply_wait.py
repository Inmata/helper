"""已受理回复的进度和一次收口；资源归艾特插件。"""
from contextlib import contextmanager
import threading

from ..integrations.reply_budget import CURRENT, ReplyBudget, ReplyExpired
from .members import SnapshotExpired


class ReplyWait:
    def __init__(self):
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.jobs = {}
        self.thread = threading.Thread(target=self._run, name="MentionChat.wait", daemon=True)
        self.thread.start()

    def accept(self, message, ctx, seconds):
        budget = ReplyBudget(message.received_at, seconds)
        personality = ctx.personality(message)
        snapshot = ctx.members.conversation_snapshot(message)
        with self.lock:
            self.jobs[id(budget)] = (budget, message, ctx, snapshot, personality)
        return budget

    def wrap(self, turn, budget):
        try:
            while True:
                token = CURRENT.set(budget)
                try:
                    budget.remaining(1)
                    next(turn)
                except StopIteration:
                    return
                finally:
                    CURRENT.reset(token)
                yield
        except ReplyExpired:
            self.finish(budget, "这次等待超时或处理次数已用完，还没完成回答；可以回复‘重试’继续。")
        except SnapshotExpired:
            return
        except Exception as error:
            with self.lock:
                job = self.jobs.get(id(budget))
            if job is not None:
                job[2].logger.exception("回复任务失败：%s", type(error).__name__)
            self.finish(budget, "这次处理失败，还没完成回答；回复‘重试’可重新处理原问题。")
            raise
        finally:
            turn.close()
            with self.lock:
                self.jobs.pop(id(budget), None)
            with budget.lock:
                budget.closed = True

    def finish(self, budget, text):
        with self.lock:
            job = self.jobs.pop(id(budget), None)
        if job is None:
            return
        with budget.lock:
            budget.closed = True
            if budget.sent or budget.sending:
                return
            text = budget.ready or text
        self._send(job, text)

    def _send(self, job, text, *, progress=False):
        budget, message, ctx, snapshot, personality = job
        @contextmanager
        def guard():
            with ctx.response_guard(snapshot, personality):
                if self.stopped.is_set():
                    raise SnapshotExpired("插件已停止")
                if progress:
                    with budget.lock:
                        if budget.closed or budget.sent or budget.sending:
                            raise SnapshotExpired("本轮已结束")
                yield
        try:
            from .utterance import QA, speak
            display = ctx.members.at_display_name(message.sender_id, message.chat_id) or "群友"
            speak(text, tier=QA, byte_budget=1023 - len(("@" + display + "\u2005\n").encode("utf-8")),
                  send_at=lambda part: ctx.reply_at(message, part, delivery_guard=guard))
        except (SnapshotExpired, RuntimeError):
            ctx.logger.info("回复状态未提交")

    def _run(self):
        while not self.stopped.wait(0.25):
            with self.lock:
                jobs = tuple(self.jobs.values())
            for job in jobs:
                budget = job[0]
                with budget.lock:
                    now = budget.clock()
                    expired = now >= budget.deadline
                    notice = not (budget.closed or budget.sent or budget.sending or budget.progress) and now >= budget.notice_at
                    if notice:
                        budget.progress = True
                if expired:
                    self.finish(budget, "这次等待超时，还没完成回答；回复‘重试’可重新处理原问题。")
                elif notice:
                    self._send(job, f"还在{budget.stage}，完成后回复你。", progress=True)

    def close(self):
        self.stopped.set()
        with self.lock:
            for job in self.jobs.values():
                job[0].closed = True
            self.jobs.clear()
        self.thread.join()
