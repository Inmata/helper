"""群生图的独立任务：请求归属、参考图、后台生成与图片交付。"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import queue
import re
import threading
import time
from uuid import uuid4

from wxbot_framework.conversation.members import SnapshotExpired
from wxbot_framework.conversation.mention_tools import _within
from wxbot_framework.conversation.tool_router import JevToolRouter
from wxbot_framework.integrations.magpie_images import MagpieImageRejectedError, MagpieImages, REQUEST_SECONDS
from wxbot_framework.integrations.magpie_tools import MagpieToolsError
from wxbot_framework.recent_images import RECENT_IMAGES, READY

COMMAND = "/生图"
TASK_SECONDS = 300
OUTPUT_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "generated_images"
REFERENCE_HINT = re.compile(r"这[张幅个].{0,6}(?:图|照)|(?:原图|这张|这幅|改图)|(?:背景|画风).{0,3}(?:改|换)|(?:改|换).{0,3}(?:背景|画风)")


@dataclass
class ImageJob:
    message: object
    ctx: object
    prompt: str
    reference: object
    guard: object
    key: tuple
    filename: str
    deadline: float
    task_id: str = ""
    stage: str = "queued"


class ImageReply:
    def __init__(self):
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._intent_lock = threading.Lock()
        self._intents = OrderedDict()
        self._pending = OrderedDict()
        self._members = set()
        self._queue = queue.Queue()
        self._thread = None
        self._client = None

    def start(self, ctx):
        self._client = MagpieImages(ctx.magpie, OUTPUT_DIR)
        self._thread = threading.Thread(target=self._run, name="MentionChat.images", daemon=True)
        self._thread.start()

    def busy(self):
        with self._lock:
            return bool(self._members)

    def close(self):
        self.stopped.set()
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
                mode = _within(lambda: JevToolRouter(ctx.jev).image_request(text), 3, self.stopped)
            except (ValueError, TimeoutError, SnapshotExpired):
                mode = ""
            self._intents[identity] = mode
            if len(self._intents) > 128:
                self._intents.popitem(last=False)
            return mode

    def request(self, message, ctx, prompt, *, mode="command", cold=None):
        if message.is_self or self.stopped.is_set() or (cold is not None and cold.active(message)):
            return
        if not 1 <= len(prompt.strip()) <= 2000:
            ctx.reply(message, "用法：/生图 描述，例如 /生图 一只水彩橘猫。描述最多 2000 字；引用一张图片可以改图。")
            return
        if "group.image.send" not in ctx.capabilities:
            ctx.reply(message, "当前微信连接不支持发送图片。")
            return
        quote = message.data.get("quote") or {}
        quoted = quote.get("ref_type") == "3"
        wants_reference = quoted or mode == "edit" or (mode == "command" and REFERENCE_HINT.search(prompt))
        reference = None
        if wants_reference:
            if "group.image.receive" not in ctx.capabilities:
                ctx.reply(message, "当前连接收不到参考图，请先用文字描述要画的内容。")
                return
            if quoted:
                reference = RECENT_IMAGES.quoted(message.chat_id, svrid=quote.get("svrid") or "",
                                                  md5=quote.get("image_md5") or "")
            else:
                candidates = RECENT_IMAGES.recent(message.chat_id, message.sender_id,
                                                   before=message.received_at, within=120)
                if len(candidates) == 1:
                    reference = candidates[0]
            if reference is None:
                ctx.reply(message, "请引用要修改的那张图片；没有收录的话，先把原图重新发一下。")
                return
        member = message.session_id, message.chat_id, message.sender_id
        key = (*member, prompt.strip(), reference.key if reference else "")
        snapshot, personality = ctx.members.conversation_snapshot(message), ctx.personality(message)

        @contextmanager
        def guard():
            with ctx.response_guard(snapshot, personality):
                if self.stopped.is_set() or (cold is not None and cold.active(message)):
                    raise SnapshotExpired("生图回复已取消")
                yield

        with self._lock:
            previous = self._pending.get(key)
            if key in self._pending and (previous is None or message.received_at <= previous):
                return
            if member in self._members:
                busy = "你上一张图还在处理，等它完成后再发新的请求吧。"
            elif len(self._members) >= 3:
                busy = "画图队列满了，稍后再试一下。"
            else:
                busy = ""
                queued = bool(self._members)
                self._members.add(member)
                self._pending[key] = None
                self._pending.move_to_end(key)
        if busy:
            ctx.reply(message, busy, delivery_guard=guard)
            return
        job = ImageJob(message, ctx, prompt.strip(), reference, guard, key, uuid4().hex,
                       time.monotonic() + TASK_SECONDS)
        try:
            if not ctx.reply_at(message, "已排队，画好后发给你。" if queued else "收到，画好后发给你。",
                                delivery_guard=guard):
                self._finish(job)
                return
        except Exception:
            self._finish(job)
            raise
        self._queue.put(job)

    def _finish(self, job):
        with self._lock:
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
            except (MagpieToolsError, OSError, ValueError, TimeoutError, RuntimeError) as error:
                job.ctx.logger.warning("生图失败：task=%s stage=%s remote=%s error=%s",
                                       job.filename, job.stage, job.task_id, type(error).__name__)
                if job.stage == "sending":
                    text = "图已经画好，但这次图片没发成功。"
                elif isinstance(error, MagpieImageRejectedError):
                    text = "这次请求被生图服务的安全审核拒绝，未生成图片。请调整图片内容后再试。"
                elif isinstance(error, TimeoutError):
                    text = "这张图等待超时了，这次先停止等待。"
                elif job.stage == "reference":
                    text = "参考图没能下载完成，请重新发送原图后再试。"
                else:
                    text = "这次没能取得生成图片，请稍后再试。"
                try:
                    job.ctx.reply_at(job.message, text, delivery_guard=job.guard)
                except (SnapshotExpired, RuntimeError):
                    pass
            finally:
                self._finish(job)

    def _generate(self, job):
        with job.guard():
            pass
        reference = None
        if job.reference is not None:
            job.stage = "reference"
            record = RECENT_IMAGES.wait(job.reference, 6, stopped=self.stopped)
            if record.status != READY:
                raise ValueError("参考图未就绪")
            reference = record.path
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
                             if job.task_id else self._client.submit(job.prompt, filename=job.filename,
                                                                    reference=reference, timeout=timeout),
                             timeout, self.stopped)
            if result.path is not None:
                job.stage = "sending"
                if not job.ctx.reply_at(job.message, "画好了。", delivery_guard=job.guard):
                    raise RuntimeError("发图提示未提交")
                if not job.ctx.reply_image(job.message, str(result.path), delivery_guard=job.guard):
                    raise RuntimeError("图片未提交")
                job.ctx.logger.info("生图已发送：task=%s remote=%s", job.filename, job.task_id)
                return
            job.task_id = result.task_id
            job.stage = "waiting"
            job.ctx.logger.info("生图等待：task=%s remote=%s", job.filename, job.task_id)
            self.stopped.wait(max(0, 1 - (time.monotonic() - started)))
