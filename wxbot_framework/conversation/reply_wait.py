"""已受理回复的进度和一次收口；资源归艾特插件。"""
from collections import OrderedDict
from contextlib import contextmanager
import threading
import time

from ..integrations.reply_budget import CURRENT, ReplyBudget, ReplyExpired
from .members import SnapshotExpired
from .turns import CLARIFICATION_SECONDS, FOLLOW_SECONDS, judge_turn, turn_key


class ReplyWait:
    def __init__(self, status):
        self.status = status
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.jobs = {}
        self.outcomes = OrderedDict()
        self.thread = threading.Thread(target=self._run, name="MentionChat.wait", daemon=True)
        self.thread.start()

    def accept(self, message, ctx, seconds, *, retryable=False):
        budget = ReplyBudget(message.received_at, seconds)
        personality = ctx.personality(message)
        snapshot = ctx.members.conversation_snapshot(message)
        budget.source_id = snapshot.anchor_id
        with self.lock:
            self.jobs[id(budget)] = (budget, message, ctx, snapshot, personality, retryable)
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
            self.finish(budget, "本轮达到处理时限或调用上限，回答未完成")
        except SnapshotExpired:
            return
        except Exception as error:
            with self.lock:
                job = self.jobs.get(id(budget))
            if job is not None:
                job[2].logger.exception("回复任务失败：%s", type(error).__name__)
            self.finish(budget, "本轮处理失败，回答未完成")
            raise
        finally:
            turn.close()
            with self.lock:
                job = self.jobs.pop(id(budget), None)
                with budget.lock:
                    budget.closed = True
                    if job is not None and budget.sent:
                        self.outcomes.pop(turn_key(job[1]), None)

    def finish(self, budget, reason, *, blocked=False):
        with self.lock:
            job = self.jobs.pop(id(budget), None)
            if job is None:
                return
            _, message, ctx, _, personality, retryable = job
            key = turn_key(message)
            with budget.lock:
                budget.closed = True
                draft = budget.ready
                if blocked or (draft is not None and not budget.draft_is_current(draft)):
                    draft = None
                if budget.sent or budget.sending or draft is not None:
                    self.outcomes.pop(key, None)
                if budget.sent or budget.sending:
                    return
                if budget.attempted:
                    draft = None
                    reason = "回复发送结果未确认，已停止补发"
            if draft is None:
                facts = {"结果": reason}
                if retryable and not blocked and not budget.attempted:
                    facts["可用操作"] = "艾特机器人说「重试」，可重新处理原问题"
                self.outcomes[key] = (job, facts, time.monotonic(), blocked)
                self.outcomes.move_to_end(key)
                if len(self.outcomes) > 64:
                    self.outcomes.popitem(last=False)
        if draft is not None:
            self._send(job, draft)
        else:
            self.status.submit(message, ctx, facts, personality=personality, guard=self._guard(job),
                               question="" if blocked else None)

    def followup(self, message, ctx, question, *, addressed):
        key = turn_key(message)
        with self.lock:
            outcome = self.outcomes.get(key)
        if outcome is None:
            return False
        job, facts, ended, blocked = outcome
        elapsed = message.received_at - job[1].received_at
        if elapsed <= 0:
            return False
        retry = addressed and question in ("重试", "再试一次")
        blocked_retry = retry and blocked and elapsed <= 300
        if retry and not blocked_retry:
            return False
        window = CLARIFICATION_SECONDS if addressed else FOLLOW_SECONDS
        if not blocked_retry and (message.received_at - ended > window or not question):
            return False

        @contextmanager
        def guard():
            with self._guard(job)():
                with self.lock:
                    if self.outcomes.get(key) is not outcome:
                        raise SnapshotExpired("上一轮处理结果已变化")
                yield

        try:
            with guard():
                pass
            if not blocked_retry and not judge_turn(ctx.jev, "status", "上一轮请求处理结果",
                                                    previous=facts["结果"], new=question):
                return False
        except (SnapshotExpired, ValueError):
            return False
        if blocked_retry:
            facts = {**facts, "本次操作": "没有重新提交已拦截的请求"}
        self.status.submit(message, ctx, facts, personality=job[4], guard=guard, question=question, at=addressed)
        return True

    def _guard(self, job, *, stage=None, revision=None):
        budget, _, ctx, snapshot, personality, _ = job
        @contextmanager
        def guard():
            with ctx.response_guard(snapshot, personality):
                if self.stopped.is_set():
                    raise SnapshotExpired("插件已停止")
                if stage is not None:
                    with budget.lock:
                        if (budget.closed or budget.sent or budget.sending or budget.stage != stage or
                                budget.revision != revision or
                                budget.clock() >= budget.deadline):
                            raise SnapshotExpired("回复阶段已变化")
                yield
        return guard

    def _send(self, job, draft):
        budget, message, ctx, _, _, _ = job

        @contextmanager
        def guard():
            with self._guard(job)():
                if not budget.draft_is_current(draft):
                    raise SnapshotExpired("已有新条件，原草稿已失效")
                yield

        try:
            from .utterance import QA, speak
            display = ctx.members.at_display_name(message.sender_id, message.chat_id) or "群友"
            def send_at(part):
                token = CURRENT.set(budget)
                try:
                    submitted = ctx.reply_at(message, part, delivery_guard=guard)
                finally:
                    CURRENT.reset(token)
                if submitted:
                    budget.submitted()
                    if draft.on_sent is not None:
                        with self._guard(job)():
                            draft.on_sent(part)
                return submitted
            speak(draft.text, tier=QA, byte_budget=1023 - len(("@" + display + "\u2005\n").encode("utf-8")),
                  send_at=send_at)
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
                    notice = not (budget.closed or budget.sent or budget.sending or budget.attempted or budget.progress) and now >= budget.notice_at
                    if notice:
                        budget.progress = True
                        stage = budget.stage
                        revision = budget.revision
                if expired:
                    self.finish(budget, "本轮等待超时，回答未完成")
                elif notice:
                    _, message, ctx, _, personality, _ = job
                    self.status.submit(message, ctx, {"当前阶段": stage}, personality=personality,
                                       guard=self._guard(job, stage=stage, revision=revision))

    def close(self):
        self.stopped.set()
        with self.lock:
            for job in self.jobs.values():
                job[0].closed = True
            self.jobs.clear()
            self.outcomes.clear()
        self.thread.join()
