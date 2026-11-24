"""群生图的独立任务：请求归属、参考图、后台生成与图片交付。"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
import queue
import re
import threading
import time
from uuid import uuid4

from wxbot_framework.conversation.members import SnapshotExpired
from wxbot_framework.conversation.mention_tools import _within
from wxbot_framework.conversation.turns import PHOTO_TOPIC, turn_key
from wxbot_framework.integrations.jev import JevError
from wxbot_framework.integrations.llm import LLMConnectionError, LLMContentBlocked, LLMFormatError
from wxbot_framework.integrations.magpie_images import MagpieImageRejectedError, MagpieImages, REQUEST_SECONDS
from wxbot_framework.integrations.magpie_tools import MagpieToolsError, MagpieToolsUnavailableError
from wxbot_framework.integrations.reply_budget import CURRENT, ReplyBudget
from wxbot_framework.messaging.mentions import strip_at_labels
from wxbot_framework.recent_images import ImageRecord, RECENT_IMAGES, READY
from .image_intent import FORMAT_SECONDS, choose_format
from .image_plan import (CHAT_PHOTO_SOURCE, ImageProfiles, fallback_image, image_sources, prepare_image,
                         select_image_sources, uses_previous_picture)

TASK_SECONDS = 300
RECEIPT_SECONDS = 25
PLAN_SECONDS = 75
SOURCE_SECONDS = 15
OUTPUT_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "generated_images"
PAIR_HINT = re.compile(r"(?:这|那|上面|前面|刚(?:刚|才)?发)(?:的)?[两二2]\s*[张幅]|"
                       r"(?:参考|用|结合|合并|融合|把|将)\s*[两二2]\s*[张幅](?:(?:参考)?[图照]|都)")
FIRST_IMAGE = re.compile(r"第\s*[一1]\s*[张幅]|图\s*[一1](?!\d)")
SECOND_IMAGE = re.compile(r"第\s*[二2]\s*[张幅]|图\s*[二2](?!\d)")
RECENT_REFERENCE = re.compile(r"(?:我|刚(?:刚|才)?|我刚(?:刚|才)?)发(?:过)?[的那这一]")
EDIT_HINT = re.compile(r"这[张幅个].{0,8}(?:图|照)?.{0,8}(?:改|换|调整|重绘|去掉|保留)|"
                       r"(?:改|换|调整|重绘|纠正|修复).{0,12}(?:这[张幅]|原图)|"
                       r"^(?:请|帮我|给我)?(?:把|给|只)?(?:改图|(?:改|换|调整)(?:一下|个)?(?:背景|画风)|"
                       r"(?:背景|画风).{0,4}(?:改|换|调整))")


@dataclass
class ImageJob:
    message: object
    ctx: object
    prompt: str
    references: tuple[ImageRecord, ...]
    snapshot: object
    guard: object
    key: tuple
    filename: str
    deadline: float
    personality: object
    task_id: str = ""
    stage: str = "queued"
    failure: str = ""
    question_text: str = ""
    revisions: tuple = ()
    previous_prompt: str = ""
    source_id: int = 0
    clarification_until: float = 0
    missing_reference: bool = False
    quoted_image: bool = False
    previous_image: bool = False
    recent_pictures: tuple[ImageRecord, ...] = ()
    material_question: str = ""
    replacement_role: str = ""
    material_sources: dict = field(default_factory=dict)
    required_references: int = 0
    edit: bool = False
    plan: object = None
    cancelled: object = field(default_factory=threading.Event)


class ImageReply:
    def __init__(self):
        self.stopped = threading.Event()
        self._lock = threading.RLock()
        self._intent_lock = threading.Lock()
        self._intents = OrderedDict()
        self._format_questions = OrderedDict()
        self._pending = OrderedDict()
        self._members = set()
        self._queue = queue.Queue()
        self._thread = None
        self._client = None
        self._jobs = OrderedDict()
        self._status = None

    def _notify(self, job, message, ctx, facts, *, stages, question=None, receipt_deadline=None):
        @contextmanager
        def guard():
            with job.guard():
                with self._lock:
                    if self._jobs.get(job.key[:3]) is not job or job.stage not in stages:
                        raise SnapshotExpired("生图状态已变化")
                yield

        self._status.submit(message, ctx, facts, personality=job.personality, guard=guard,
                            question=question, receipt_deadline=receipt_deadline)

    def status(self, message, ctx, text):
        member = message.session_id, message.chat_id, message.sender_id
        with self._lock:
            job = self._jobs.get(member)
        if job is None:
            return False
        question = {"instructions": "判断当前发言是否在查询或恢复查询自己已有的生图任务。",
                    "true": "询问已有图片在哪、是否完成、任务进度，或简短要求重试原任务。",
                    "false": "要求重新画一张、再出一版、换参考图重画、修改图片或进行其他聊天。"}
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
                  "needs_input": "已发出追问，等待补充信息" if job.question_text else "缺少素材，补充提示尚未确认发出",
                  "submitting": "提交生成请求",
                  "waiting": "等待生成服务返回图片", "sending": "提交图片到群聊",
                  "complete": "已发图到本群",
                  "failed": "生图失败", "expired": "等待超时", "cancelled": "任务取消",
                  "delivery_unknown": "图片发送结果不明，已停止补发"}
        if resuming:
            self._notify(job, message, ctx, {"本次操作": "已恢复查询原任务，没有再次提交生图"},
                         stages=("queued", "reference", "submitting", "waiting", "sending"), question=text)
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
            return None
        identity = ((message.dispatch_epoch, message.dispatch_sequence) if message.dispatch_sequence
                    else (message.session_id, id(message)))
        with self._intent_lock:
            if identity in self._intents:
                return self._intents[identity]
        snapshot = ctx.members.conversation_snapshot(message)
        member = turn_key(message)
        with self._lock:
            last_job = self._jobs.get(member)
        previous = ""
        if last_job and 0 < message.received_at - last_job.message.received_at <= 1800:
            try:
                with last_job.guard():
                    previous = "\n".join(filter(None, (last_job.previous_prompt, last_job.prompt, *last_job.revisions)))[-2000:]
            except SnapshotExpired:
                pass
        # 预判补话时可能身处上一轮预算；路由单独限时，正文仍有完整调用额度。
        outer = CURRENT.get()
        seconds = outer.remaining(FORMAT_SECONDS) if outer else FORMAT_SECONDS
        token = CURRENT.set(ReplyBudget(time.monotonic(), seconds))
        try:
            decision, origin, error = choose_format(ctx, message, snapshot, text, previous, self.stopped)
            intent = decision if decision.mode != "text" else None
            ctx.logger.info("生图分流：消息=%s 来源=%s 动作=%s 异常=%s",
                            snapshot.anchor_id, origin, decision.mode, error or "-")
        finally:
            CURRENT.reset(token)
        if outer:
            outer.remaining(1)
        with self._intent_lock:
            self._intents[identity] = intent
            if len(self._intents) > 128:
                self._intents.popitem(last=False)
        return intent

    def format_answer(self, message, ctx, text, source):
        key = turn_key(message)
        with self._lock:
            pending = self._format_questions.get(key)
        if pending is None or message.is_self:
            return None
        original, prompt, guard, asked_at, asked_text = pending
        quote = message.data.get("quote") or {}
        if quote and (ctx.members.account not in (quote.get("fromusr"), quote.get("chatusr"))
                      or strip_at_labels(quote.get("text") or "") != asked_text):
            return None
        if not source and (message.data.get("at_members") or message.text.lstrip().startswith("@")):
            return None
        choice = re.fullmatch(r"(?:我想要|我要|要|请|就)?(?:画成|做成|生成|写成)?(图片|图|文字)(?:版|就行|就好|吧)?",
                              text.strip(" ！!。"))
        if not choice:
            if source:
                with self._lock:
                    self._format_questions.pop(key, None)
            return None
        with self._lock:
            self._format_questions.pop(key, None)
        if not 0 <= message.received_at - asked_at <= TASK_SECONDS:
            return None
        try:
            with guard():
                pass
        except SnapshotExpired:
            return None
        data = {**message.data, "quote": original.data.get("quote")}
        mode = "text" if choice[1] == "文字" else "create"
        ctx.logger.info("生图形式确认：消息=%s 原消息=%s 动作=%s", ctx.members.message_id(message),
                        ctx.members.message_id(original), mode)
        return replace(message, data=data), prompt, mode

    def archive_followup(self, message, ctx, text, *, cold=None):
        if not re.fullmatch(r"(?:(?:用|就用|都用)?(?:库里|档案库里|照片库里)(?:的|面的)?(?:照片|档案照)?"
                            r"|都用上|用.{1,32}的档案照)[啊呀吧。！!]*", text.strip()):
            return False
        key = turn_key(message)
        with self._lock:
            job = self._jobs.get(key)
            if not job or not (PHOTO_TOPIC.search(job.prompt) or re.search("众人|群友|全员", job.prompt) or "档案照" in job.failure):
                return False
            if job.stage in ("submitting", "waiting", "sending") and job.plan and "人物外貌参考：" in job.plan.instruction and "都用上" not in text:
                return True
            if job.stage not in ("queued", "reference", "planning", "needs_input"):
                return False
            if job.stage == "needs_input" and time.monotonic() >= job.clarification_until:
                return False
        try:
            with job.guard():
                pass
        except SnapshotExpired:
            return False
        self.request(message, ctx, job.prompt, cold=cold, prior=job, supplement=text)
        return True

    def followup(self, message, ctx, text, *, addressed=False, can_revise=False, cold=None):
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
        quote_added = (quote.get("ref_type") == "3" and text == job.prompt and not job.quoted_image
                       and job.stage in ("queued", "reference", "planning", "needs_input"))
        if quote_added:
            if job.stage == "needs_input" and time.monotonic() >= job.clarification_until:
                return False
            self.request(message, ctx, job.prompt, cold=cold, prior=job)
            return True
        if job.stage == "needs_input":
            author_is_bot = ctx.members.account in (quote.get("fromusr"), quote.get("chatusr"))
            answered = bool(job.question_text and author_is_bot and strip_at_labels(quote.get("text") or "").strip() == job.question_text)
            supplied_image = (addressed and quote.get("ref_type") == "3" and
                              (job.material_question or len(job.references) < max(1, job.required_references)))
            if time.monotonic() >= job.clarification_until or not (answered or supplied_image):
                return False
        elif can_revise and active and job.stage in ("queued", "reference", "planning", "submitting", "waiting"):
            if text in (job.prompt, *job.revisions):
                return False
            question = {"instructions": "判断当前发言是否明确要求把一项变更用到正在做的这张图上。",
                        "true": "要求在原图上改变、追加或删除画面条件，例如「刚才那张猫图，背景换成蓝色」。",
                        "false": "另做一张独立的图、只提到相同人物或题材、问现实里的事情、询问进度或指代不清。"}
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

    def request(self, message, ctx, prompt, *, mode="command", cold=None, prior=None, supplement="", previous_prompt=""):
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

        member = turn_key(message)
        with self._lock:
            self._format_questions.pop(member, None)
        if mode == "clarify":
            @contextmanager
            def ask_guard():
                with guard():
                    budget = CURRENT.get()
                    if budget is None:
                        yield
                    else:
                        with budget.delivery():
                            yield
            text = self._status.compose(message, ctx, {"待确认": "用一句问句请对方从图片、文字两种形式里选一种，带上两个选项"},
                                        personality=personality, guard=guard, choices=("图", "文字"))
            if text and ctx.reply_at(message, text, delivery_guard=ask_guard):
                with self._lock:
                    self._format_questions[member] = (message, prompt, guard, time.monotonic(), text)
                    if len(self._format_questions) > 64:
                        self._format_questions.popitem(last=False)
            return
        if not 1 <= len(prompt.strip()) <= 2000:
            inform({"用法": "/生图 描述，描述须为 1–2000 字；引用一张图可以改图"})
            return
        if "group.image.send" not in ctx.capabilities:
            inform({"原因": "当前微信连接不支持发图，本次未受理"})
            return
        quote = message.data.get("quote") or {}
        if prior and prior.required_references == 2 and prior.stage == "needs_input" and quote.get("ref_type") != "3":
            original_quote = prior.message.data.get("quote") or {}
            if original_quote.get("ref_type") == "3":
                quote = original_quote
        quoted = quote.get("ref_type") == "3"
        text = supplement or prompt
        edit = mode == "edit" or bool((prior is None or quoted) and EDIT_HINT.search(text))
        paired = bool(PAIR_HINT.search(text) or (FIRST_IMAGE.search(text) and SECOND_IMAGE.search(text))
                      or (quoted and RECENT_REFERENCE.search(text)))
        previous_image = not paired and uses_previous_picture(prompt)
        required_references = max(2 if paired else int(quoted or edit or previous_image),
                                  prior.required_references if prior else 0)
        references = prior.references if prior else ()
        recent_pictures = prior.recent_pictures if prior else ()
        if "group.image.receive" in ctx.capabilities and (not prior or supplement):
            recent_pictures = RECENT_IMAGES.preceding(message.chat_id, before=message.received_at, within=120)
        missing_reference = prior.missing_reference if prior else False
        if quoted and "group.image.receive" not in ctx.capabilities:
            inform({"原因": "当前连接收不到引用的群图", "可用操作": "可使用本群档案照或文字描述"})
            return
        if ("group.image.receive" in ctx.capabilities
                and (prior is None or quoted or (required_references == 2 and prior.stage == "needs_input"))):
            candidates = RECENT_IMAGES.recent(message.chat_id, message.sender_id, before=message.received_at,
                                               within=120) if required_references == 2 or not quoted else ()
            if quoted:
                RECENT_IMAGES.restore_sent(ctx.members, message.chat_id, quote=quote, source_id=ctx.members.message_id(message))
                primary = RECENT_IMAGES.quoted(message.chat_id, svrid=quote.get("svrid") or "",
                                               md5=quote.get("image_md5") or "", members=ctx.members)
                missing_reference = primary is None
                references = (primary,) if primary else ()
                if primary and required_references == 2:
                    references += tuple(record for record in candidates if record.key != primary.key)[-1:]
            elif previous_image:
                references = recent_pictures[-1:]
            else:
                references = candidates[-2:]
        revisions = prior.revisions if prior else ()
        if supplement:
            revisions = (*revisions, supplement)
        key = (*member, prompt.strip(), quoted, tuple(record.key for record in references), revisions)

        with self._lock:
            previous = self._pending.get(key)
            if key in self._pending and (previous is None or message.received_at <= previous):
                return
            if prior is not None and self._jobs.get(member) is not prior:
                return
            if prior is not None and not supplement and prior.stage not in ("queued", "reference", "planning", "needs_input"):
                return
            if member in self._members and prior is None:
                busy = "同一人已有图片任务在处理，新任务未受理"
            elif member not in self._members and len(self._members) >= 3:
                busy = "生图队列满，新任务未受理"
            else:
                busy = ""
                queued = bool(self._members - {member})
                self._members.add(member)
                self._pending[key] = None
                self._pending.move_to_end(key)
                job = ImageJob(message, ctx, prompt.strip(), references, snapshot, guard, key, uuid4().hex,
                               time.monotonic() + TASK_SECONDS, personality,
                               revisions=revisions, source_id=prior.source_id if prior else snapshot.anchor_id,
                               previous_prompt=prior.previous_prompt if prior else previous_prompt,
                               missing_reference=missing_reference,
                               quoted_image=quoted or bool(prior and prior.quoted_image),
                               previous_image=previous_image,
                               recent_pictures=recent_pictures,
                               replacement_role=prior.material_question if prior and quoted else "",
                               material_sources=(prior.material_sources.copy()
                                                 if prior and prior.material_question and quoted and
                                                 (not supplement or re.fullmatch(r"(?:就用|补上|补发|用)?这张(?:图|照片)?[。！!]*", supplement))
                                                 else {}),
                               required_references=required_references,
                               edit=edit or bool(prior and (prior.edit or (prior.plan and prior.plan.edit))))
                if prior is not None:
                    prior.cancelled.set()
                self._jobs[member] = job
                self._jobs.move_to_end(member)
                if len(self._jobs) > 64:
                    self._jobs.popitem(last=False)
        if busy:
            ctx.logger.info("生图未入队：消息=%s 原因=%s", snapshot.anchor_id, busy)
            inform({"未受理原因": busy})
            return
        ctx.logger.info("生图入队：消息=%s 原消息=%s task=%s mode=%s", snapshot.anchor_id, job.source_id, job.filename, mode)
        self._queue.put(job)
        if prior is None:
            receipt_deadline = message.received_at + RECEIPT_SECONDS
            if queued:
                self._notify(job, message, ctx, {"生图受理": "已接单，前面还有生图任务"}, stages=("queued",),
                             receipt_deadline=receipt_deadline)
            else:
                def receipt():
                    return {"生图受理": "生图服务已接单，正在生成" if job.stage == "waiting" else "已收到本次绘图要求"}

                self._notify(job, message, ctx, receipt, stages=("queued", "reference", "planning", "submitting", "waiting"),
                             receipt_deadline=receipt_deadline)

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
                elif isinstance(error, TimeoutError):
                    job.failure = "等待生图超时，已停止等待；远端结果未知" if job.task_id else "等待超时，未取到图片"
                elif job.stage == "reference":
                    job.failure = "参考图下载未完成；可重发原图后引用"
                else:
                    job.failure = "生图服务出错，未取到图片"
                job.stage = ("delivery_unknown" if job.stage == "sending" else
                             "expired" if isinstance(error, TimeoutError) else "failed")
                unavailable = (TimeoutError, LLMConnectionError, MagpieToolsUnavailableError)
                if not isinstance(error, unavailable) and not isinstance(error.__cause__, unavailable):
                    self._notify(job, job.message, job.ctx, {"结果": job.failure}, stages=(job.stage,))
            finally:
                self._finish(job)

    def _generate(self, job):
        with job.guard():
            pass
        if job.plan is None:
            ready_references = []
            if job.references:
                job.stage = "reference"
                download_deadline = time.monotonic() + 6
                for index, image in enumerate(job.references):
                    wait = max(0, download_deadline - time.monotonic()) if job.required_references else 0
                    record = RECENT_IMAGES.wait(image, wait, stopped=job.cancelled)
                    if record.status == READY:
                        ready_references.append(record)
                    elif index == 0 and (job.message.data.get("quote") or {}).get("ref_type") == "3":
                        job.missing_reference = True
                job.references = tuple(ready_references)
            references = tuple(record.path for record in ready_references)
            job.stage = "planning"
            profiles = ImageProfiles(job.ctx, job.snapshot, "\n".join((job.prompt, *job.revisions)))
            options = {"missing_reference": job.missing_reference, "required_references": job.required_references,
                       "edit": job.edit}
            sources = image_sources(job, profiles, references)
            request = "\n".join(filter(None, (job.previous_prompt, job.prompt, *job.revisions)))
            photo_topic = PHOTO_TOPIC.search(request)
            recover_sources = bool(profiles.portraits.people or photo_topic) and (not job.missing_reference or bool(job.revisions))
            now = time.monotonic()
            budget = ReplyBudget(now, min(PLAN_SECONDS, job.deadline - now))
            budget.llm_limit += int(recover_sources)
            token = CURRENT.set(budget)
            try:
                prepare_until = budget.deadline - (SOURCE_SECONDS if recover_sources else 0)
                with budget.work("整理绘图要求", prepare_until):
                    job.plan, question = _within(lambda: prepare_image(
                        job.ctx, job.message, job.snapshot, sources, references, job.guard, profiles, job=job, **options),
                        budget.remaining(PLAN_SECONDS, until=prepare_until), job.cancelled)
            except (LLMConnectionError, LLMFormatError, TimeoutError) as error:
                if job.material_sources:
                    raise
                job.ctx.logger.info("生图整理回退：task=%s error=%s", job.filename, type(error).__name__)
                selection = {}
                if recover_sources:
                    try:
                        selection = _within(lambda: select_image_sources(job.ctx.llm, sources),
                                            budget.remaining(SOURCE_SECONDS), job.cancelled)
                    except (LLMConnectionError, LLMFormatError, TimeoutError) as source_error:
                        job.ctx.logger.info("生图选材未完成：task=%s error=%s", job.filename, type(source_error).__name__)
                        if job.previous_image and profiles.mentioned:
                            raise RuntimeError("指定群图的人物用途尚未核实") from source_error
                        names, clarification = profiles.portraits.fallback_names(request)
                        if not job.quoted_image and job.required_references < 2:
                            if names:
                                count = max(job.required_references, int(bool(CHAT_PHOTO_SOURCE.search(request))))
                                selection = {"档案照片": names, "群聊参考图数": count}
                            elif clarification:
                                selection = {"澄清问题": clarification}
                            elif photo_topic and (profiles.portraits.people or re.search(r"档案|库|相册", request)):
                                raise RuntimeError("无法确认本次素材来源") from source_error
                job.plan, question = fallback_image(sources, references, profiles, selection, **options)
            finally:
                CURRENT.reset(token)
            with job.guard():
                pass
            if question:
                job.stage = "needs_input"
                job.failure = question
                job.clarification_until = time.monotonic() + TASK_SECONDS
                text = self._status.compose(job.message, job.ctx, {"待确认": question},
                                            personality=job.personality, guard=job.guard)
                if text and job.ctx.reply_at(job.message, text, delivery_guard=job.guard):
                    job.question_text = text
                else:
                    job.ctx.logger.info("生图追问未确认发出：消息=%s", job.source_id)
                return
            job.ctx.logger.info("生图要求已整理：task=%s references=%d", job.filename, len(job.plan.references))
        job.stage = "submitting"
        while True:
            with job.guard():
                pass
            started = time.monotonic()
            remaining = job.deadline - started
            if remaining <= 0:
                raise TimeoutError("生图任务等待超时")
            timeout = min(REQUEST_SECONDS, remaining)
            result = _within(lambda: self._client.result(job.task_id, filename=job.filename, timeout=timeout)
                             if job.task_id else self._client.submit(job.plan.instruction, filename=job.filename,
                                                                    references=job.plan.references, timeout=timeout),
                             timeout, job.cancelled)
            if result.path is not None:
                break
            job.task_id = result.task_id
            job.stage = "waiting"
            job.ctx.logger.info("生图等待：task=%s remote=%s", job.filename, job.task_id)
            self.stopped.wait(max(0, 1 - (time.monotonic() - started)))
        with job.guard():
            pass
        job.stage = "sending"
        if not job.ctx.reply_image(job.message, str(result.path), delivery_guard=job.guard,
                                   source_id=job.source_id, revision=len(job.revisions)):
            raise RuntimeError("图片未提交")
        job.stage = "complete"
        job.ctx.logger.info("生图已发送：消息=%s task=%s remote=%s", job.source_id, job.filename, job.task_id)
