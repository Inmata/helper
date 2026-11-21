"""群生图的独立任务：请求归属、参考图、后台生成与图片交付。"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import queue
import re
import threading
import time
from uuid import uuid4

from wxbot_framework.conversation.members import SnapshotExpired
from wxbot_framework.conversation.mention_tools import _within
from wxbot_framework.conversation.tool_router import JevToolRouter
from wxbot_framework.integrations.jev import JevError
from wxbot_framework.integrations.llm import LLMContentBlocked
from wxbot_framework.integrations.magpie_images import MagpieImageRejectedError, MagpieImages, REQUEST_SECONDS
from wxbot_framework.integrations.magpie_tools import MagpieToolsError
from wxbot_framework.integrations.reply_budget import CURRENT, ReplyBudget
from wxbot_framework.messaging.mentions import strip_at_labels
from wxbot_framework.recent_images import RECENT_IMAGES, READY
from .image_plan import prepare_image
from .image_quality import check_image, typeset_image

COMMAND = "/生图"
TASK_SECONDS = 300
PLAN_SECONDS = 75
OUTPUT_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "generated_images"
REFERENCE_HINT = re.compile(r"这[张幅个].{0,6}(?:图|照)|(?:原图|这张|这幅|改图)|(?:背景|画风).{0,3}(?:改|换)|(?:改|换).{0,3}(?:背景|画风)")


@dataclass
class ImageJob:
    message: object
    ctx: object
    prompt: str
    reference: object
    snapshot: object
    guard: object
    key: tuple
    filename: str
    deadline: float
    personality: object
    task_id: str = ""
    stage: str = "queued"
    path: str = ""
    failure: str = ""
    revisions: tuple = ()
    clarification_until: float = 0
    missing_reference: bool = False
    reference_required: bool = False
    plan: object = None
    drawing: str = ""
    repair_reference: str = ""
    cancelled: object = field(default_factory=threading.Event)


class ImageReply:
    def __init__(self):
        self.stopped = threading.Event()
        self._lock = threading.RLock()
        self._intent_lock = threading.Lock()
        self._intents = OrderedDict()
        self._pending = OrderedDict()
        self._members = set()
        self._queue = queue.Queue()
        self._thread = None
        self._client = None
        self._jobs = OrderedDict()
        self._status = None

    def _notify(self, job, message, ctx, facts, *, stages, question=None):
        @contextmanager
        def guard():
            with job.guard():
                with self._lock:
                    if self._jobs.get(job.key[:3]) is not job or job.stage not in stages:
                        raise SnapshotExpired("生图状态已变化")
                yield

        self._status.submit(message, ctx, facts, personality=job.personality, guard=guard, question=question)

    def status(self, message, ctx, text):
        member = message.session_id, message.chat_id, message.sender_id
        with self._lock:
            job = self._jobs.get(member)
        if job is None:
            return False
        question = {"instructions": "当前发言是否在询问、催促或重试自己已有的生图任务？新的独立画图描述不算。",
                    "true": "询问已有图片在哪、是否完成、任务进度或重试原任务。", "false": "其他聊天或新的生图要求。"}
        try:
            with job.guard():
                pass
            related = _within(lambda: ctx.jev.noul({"当前话语": text, "原生图要求": job.prompt}, [question], timeout=2)[0],
                              2, self.stopped) >= 0.8
        except SnapshotExpired:
            return False
        except (JevError, ValueError, TimeoutError):
            related = text.strip(" ？?！!。") in ("图呢", "画好了吗", "好了没", "画不出来", "重试")
        if not related:
            return False
        with self._lock:
            can_resume = job.stage in ("failed", "expired") and job.task_id
            resuming = "重试" in text and can_resume and member not in self._members and len(self._members) < 3
            if resuming:
                job.deadline = time.monotonic() + TASK_SECONDS
                job.stage = "queued"
                job.failure = ""
                self._members.add(member)
                self._queue.put(job)
            stage, failure = job.stage, job.failure
        stages = {"queued": "排队", "reference": "读取参考图", "planning": "整理绘图要求和相关资料",
                  "needs_input": "等待补充信息，尚未提交生成", "submitting": "提交生成请求",
                  "waiting": "等待生成服务返回图片", "checking": "核对生成图片", "sending": "提交图片到群聊",
                  "complete": "已发图到本群",
                  "failed": "生图失败", "expired": "等待超时", "cancelled": "任务取消",
                  "delivery_unknown": "图片发送结果不明，已停止补发"}
        if resuming:
            self._notify(job, message, ctx, {"本次操作": "已恢复查询原任务，没有再次提交生图"},
                         stages=("queued", "reference", "submitting", "waiting", "checking", "sending"), question=text)
        else:
            facts = {"当前任务阶段": stages[stage]}
            if failure:
                facts["原因与可用操作"] = failure
            if can_resume and "重试" in text:
                facts["排队情况"] = "当前无法加入队列，本次没有重试"
            self._notify(job, message, ctx, facts, stages=(stage,), question=text)
        return True

    def start(self, ctx, status):
        self._status = status
        if "group.image.send" not in getattr(ctx, "capabilities", ()):
            return
        self._client = MagpieImages(ctx.magpie, OUTPUT_DIR)
        self._thread = threading.Thread(target=self._run, name="MentionChat.images", daemon=True)
        self._thread.start()

    def busy(self):
        with self._lock:
            return bool(self._members)

    def close(self):
        self.stopped.set()
        with self._lock:
            for job in self._jobs.values():
                job.cancelled.set()
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join()

    def intent(self, message, ctx, text):
        if self._thread is None or not text or len(text) > 2000:
            return ""
        identity = ((message.dispatch_epoch, message.dispatch_sequence) if message.dispatch_sequence
                    else (message.session_id, id(message)))
        with self._intent_lock:
            if identity in self._intents:
                return self._intents[identity]
            try:
                snapshot = ctx.members.conversation_snapshot(message)
                quote = message.data.get("quote") or {}
                context = {
                    "引用": {key: quote.get(key) for key in ("name", "text", "ref_type")},
                    "近期": [{"作者": row["speaker_name"], "内容": row["text"][:220]} for row in snapshot.history[-6:]],
                }
                mode = _within(lambda: JevToolRouter(ctx.jev).image_request(text, context=context), 3, self.stopped)
            except (ValueError, TimeoutError, SnapshotExpired):
                mode = ""
            self._intents[identity] = mode
            if len(self._intents) > 128:
                self._intents.popitem(last=False)
            return mode

    def followup(self, message, ctx, text, *, addressed=False, cold=None):
        if message.is_self or not text or len(text) > 2000:
            return False
        key = message.session_id, message.chat_id, message.sender_id
        with self._lock:
            job = self._jobs.get(key)
            active = key in self._members
        if job is None:
            return False
        quote = message.data.get("quote") or {}
        try:
            with job.guard():
                pass
        except SnapshotExpired:
            return False
        if job.stage == "needs_input":
            author_is_bot = ctx.members.account in (quote.get("fromusr"), quote.get("chatusr"))
            answered = author_is_bot and strip_at_labels(quote.get("text") or "").strip() == job.failure
            supplied_image = (addressed and job.reference_required and job.reference is None and quote.get("ref_type") == "3")
            if time.monotonic() >= job.clarification_until or not (answered or supplied_image):
                return False
        elif addressed and active and job.stage in ("queued", "reference", "planning", "submitting", "waiting", "checking"):
            if text in (job.prompt, *job.revisions):
                return False
            question = {"instructions": "判断当前发言是否明确修改或补充同一个正在做的生图任务。",
                        "true": "要求改变、追加或删除原任务里的条件。",
                        "false": "进度询问、闲聊、新的独立画图要求或不确定指哪张图。"}
            try:
                changed = _within(lambda: ctx.jev.noul({"正在做的图": job.prompt, "已收到补充": job.revisions,
                                                         "当前发言": text}, [question], timeout=2)[0], 2, self.stopped)
            except (JevError, ValueError, TimeoutError, SnapshotExpired):
                return False
            if changed < 0.8:
                return False
        else:
            return False
        self.request(message, ctx, job.prompt, cold=cold, prior=job, supplement=text)
        return True

    def request(self, message, ctx, prompt, *, mode="command", cold=None, prior=None, supplement=""):
        if message.is_self or self.stopped.is_set() or (cold is not None and cold.active(message)):
            return
        try:
            snapshot = ctx.members.conversation_snapshot(message)
        except SnapshotExpired:
            return
        personality = ctx.personality(message)

        job = None

        @contextmanager
        def guard():
            with ctx.response_guard(snapshot, personality):
                if self.stopped.is_set() or (cold is not None and cold.active(message)):
                    raise SnapshotExpired("生图回复已取消")
                with self._lock:
                    if job is not None and self._jobs.get(job.key[:3]) is not job:
                        raise SnapshotExpired("新要求已替代旧生图任务")
                    yield

        def inform(facts):
            self._status.submit(message, ctx, facts, personality=personality, guard=guard, at=False)

        if not 1 <= len(prompt.strip()) <= 2000:
            inform({"用法": "/生图 描述，描述须为 1–2000 字；引用一张图可以改图"})
            return
        if "group.image.send" not in ctx.capabilities:
            inform({"原因": "当前微信连接不支持发图，本次未受理"})
            return
        quote = message.data.get("quote") or {}
        quoted = quote.get("ref_type") == "3"
        wants_reference = quoted or mode == "edit" or (mode == "command" and REFERENCE_HINT.search(prompt))
        reference = prior.reference if prior else None
        missing_reference = prior.missing_reference if prior else False
        if wants_reference and (prior is None or quoted):
            if "group.image.receive" not in ctx.capabilities:
                inform({"原因": "当前连接收不到参考图", "可用操作": "改用纯文字描述新图"})
                return
            if quoted:
                RECENT_IMAGES.restore_sent(ctx.members, message.chat_id, quote=quote, source_id=ctx.members.message_id(message))
                reference = RECENT_IMAGES.quoted(message.chat_id, svrid=quote.get("svrid") or "",
                                                  md5=quote.get("image_md5") or "")
                missing_reference = reference is None
            else:
                candidates = RECENT_IMAGES.recent(message.chat_id, message.sender_id,
                                                   before=message.received_at, within=120)
                if len(candidates) == 1:
                    reference = candidates[0]
        member = message.session_id, message.chat_id, message.sender_id
        revisions = (*prior.revisions, supplement) if prior else ()
        key = (*member, prompt.strip(), reference.key if reference else "", revisions)

        with self._lock:
            previous = self._pending.get(key)
            if key in self._pending and (previous is None or message.received_at <= previous):
                return
            if prior is not None and self._jobs.get(member) is not prior:
                return
            if member in self._members and prior is None:
                busy = "同一人已有图片任务在处理，新任务未受理"
            elif member not in self._members and len(self._members) >= 3:
                busy = "生图队列满，新任务未受理"
            else:
                busy = ""
                queued = bool(self._members)
                self._members.add(member)
                self._pending[key] = None
                self._pending.move_to_end(key)
                job = ImageJob(message, ctx, prompt.strip(), reference, snapshot, guard, key, uuid4().hex,
                               time.monotonic() + TASK_SECONDS, personality,
                               revisions=revisions, missing_reference=missing_reference,
                               reference_required=quoted or mode == "edit" or bool(prior and prior.reference_required))
                if prior is not None:
                    prior.cancelled.set()
                self._jobs[member] = job
                self._jobs.move_to_end(member)
                if len(self._jobs) > 64:
                    self._jobs.popitem(last=False)
        if busy:
            inform({"未受理原因": busy})
            return
        stages = ("queued",) if queued else ("queued", "reference", "planning", "submitting", "waiting", "sending")
        self._notify(job, message, ctx, {"本次请求": "已受理，前面还有生图任务" if queued else "已受理生图"},
                     stages=stages)
        self._queue.put(job)

    def _finish(self, job):
        with self._lock:
            if self._jobs.get(job.key[:3]) is job:
                self._members.discard(job.key[:3])
            self._pending[job.key] = time.monotonic()
            # 活跃任务最多三个；只淘汰已经结束的旧记录。
            for key in list(self._pending):
                if len(self._pending) <= 128:
                    break
                if self._pending[key] is not None:
                    del self._pending[key]

    def _run(self):
        while (job := self._queue.get()) is not None:
            try:
                self._generate(job)
            except SnapshotExpired:
                job.ctx.logger.info("生图任务已取消：task=%s stage=%s", job.filename, job.stage)
                job.stage = "cancelled"
            except (MagpieToolsError, OSError, ValueError, TimeoutError, RuntimeError) as error:
                job.ctx.logger.warning("生图失败：task=%s stage=%s remote=%s error=%s",
                                       job.filename, job.stage, job.task_id, type(error).__name__)
                if job.stage == "sending":
                    job.failure = "图片已生成；提交结果未确认，已停止补发"
                elif isinstance(error, MagpieImageRejectedError):
                    job.failure = "生图服务的安全审核拒绝该请求，未生成图片；如再发需调整图片内容"
                elif isinstance(error, LLMContentBlocked):
                    job.failure = "生图前的需求整理被模型安全审核拦截，未提交图片生成"
                elif job.stage == "planning":
                    job.failure = "这次未能整理好绘图要求，未提交图片生成；可以重新发起生图"
                elif job.stage == "checking":
                    job.failure = "这张图没通过成品核对，本次没有发出图片"
                elif isinstance(error, TimeoutError):
                    job.failure = "等待生图超时，已停止等待；远端结果未知" if job.task_id else "等待超时，未取到图片"
                elif job.stage == "reference":
                    job.failure = "参考图下载未完成；可重发原图后引用"
                else:
                    job.failure = "生图服务出错，未取到图片"
                job.stage = ("delivery_unknown" if job.stage == "sending" else
                             "expired" if isinstance(error, TimeoutError) else "failed")
                self._notify(job, job.message, job.ctx, {"结果": job.failure}, stages=(job.stage,))
            finally:
                self._finish(job)

    def _generate(self, job):
        with job.guard():
            pass
        reference = None
        if job.plan is None:
            if job.reference is not None:
                job.stage = "reference"
                record = RECENT_IMAGES.wait(job.reference, 6, stopped=job.cancelled)
                if record.status != READY:
                    raise ValueError("参考图未就绪")
                reference = record.path
            job.stage = "planning"
            now = time.monotonic()
            budget = ReplyBudget(now, min(PLAN_SECONDS, job.deadline - now))
            token = CURRENT.set(budget)
            try:
                job.plan, question = _within(lambda: prepare_image(
                    job.ctx, job.message, job.snapshot, job.prompt, reference, job.guard,
                    revisions=job.revisions, missing_reference=job.missing_reference,
                    reference_required=job.reference_required), PLAN_SECONDS, job.cancelled)
            finally:
                CURRENT.reset(token)
            with job.guard():
                pass
            if question:
                job.stage = "needs_input"
                job.failure = question + "\n引用这条消息回复，我会接着画。"
                job.clarification_until = time.monotonic() + TASK_SECONDS
                job.ctx.reply_at(job.message, job.failure, delivery_guard=job.guard)
                return
            job.drawing = job.plan.drawing_prompt()
            job.ctx.logger.info("生图要求已整理：task=%s reference=%s", job.filename, job.plan.reference is not None)
        job.stage = "submitting"
        while True:
            with job.guard():
                pass
            remaining = job.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("生图任务等待超时")
            timeout = min(REQUEST_SECONDS, remaining)
            started = time.monotonic()
            result = _within(lambda: self._client.result(job.task_id, filename=job.filename, timeout=timeout)
                             if job.task_id else self._client.submit(job.drawing, filename=job.filename,
                                                                    reference=job.repair_reference or job.plan.reference,
                                                                    timeout=timeout),
                             timeout, job.cancelled)
            if result.path is not None:
                with job.guard():
                    pass
                job.stage = "checking"
                timeout = min(20, job.deadline - time.monotonic())
                if timeout <= 0:
                    raise TimeoutError("画面核对已超时")
                problem = _within(lambda: check_image(job.ctx.magpie, result.path, job.plan, timeout=timeout),
                                  timeout, job.cancelled)
                if problem:
                    if job.repair_reference:
                        raise ValueError("修正后的图片仍未通过画面核对")
                    job.repair_reference = str(result.path)
                    job.drawing = job.plan.drawing_prompt() + "\n请按原要求修正这一处画面问题：" + problem
                    job.task_id = ""
                    job.filename += "-repair"
                    job.stage = "submitting"
                    continue
                job.path = typeset_image(result.path, job.plan.fixed, job.plan.position) if job.plan.fixed else str(result.path)
                job.stage = "sending"
                if not job.ctx.reply_image(job.message, job.path, delivery_guard=job.guard):
                    raise RuntimeError("图片未提交")
                job.stage = "complete"
                job.ctx.logger.info("生图已发送：task=%s remote=%s", job.filename, job.task_id)
                return
            job.task_id = result.task_id
            job.stage = "waiting"
            job.ctx.logger.info("生图等待：task=%s remote=%s", job.filename, job.task_id)
            self.stopped.wait(max(0, 1 - (time.monotonic() - started)))
