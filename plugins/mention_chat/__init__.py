"""真正艾特机器人后，用本群人格回复当前发言者。"""
import json
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .._shared import idiom_state
from .cold_reply import COMMAND as COLD_COMMAND, ColdReply
from .image_intent import FORMAT_SECONDS
from .image_reply import ImageReply
from wxbot_framework.conversation.slow_replies import SlowReplies
from wxbot_framework.conversation.person_context import person_context
from wxbot_framework.conversation.reply_wait import ReplyWait, timeout_reason
from wxbot_framework.conversation.status_replies import StatusReplies
from wxbot_framework.integrations.reply_budget import CURRENT, ReplyDraft, ReplyExpired
from .voice_reply import (COMMAND as VOICE_COMMAND, READ_COMMAND as VOICE_READ_COMMAND,
                          REPLY_COMMAND as VOICE_REPLY_COMMAND, FIXED_VOICE_PROMPT,
                          VOICE_PROMPT, VoiceReply, voice_reply_format)

from wxbot_framework.conversation.dialogue import SYSTEM, Dialogue, resolve_reply_quote
from wxbot_framework.conversation.context_tools import MentionContext, SELF_QUERY_HINTS, build_default_tools, prefetch_mention_context
from wxbot_framework.conversation.answer_material import gather_answer_material, material_needs, supports_current_answer
from wxbot_framework.conversation.correction_capture import capture_corrections
from wxbot_framework.conversation.correction_lookup import CorrectionMatches, candidates as correction_candidates, route as route_corrections
from wxbot_framework.conversation.mention_tools import (IMAGE_WINDOW_SECONDS,
                                                        SEARCH_RETRY_SECONDS, MentionTools, ToolOutcome, ToolResult,
                                                        previous_question, public_search_topic, read_tool_options,
                                                        strip_links)
from wxbot_framework.conversation.reply_review import VOICE_CAPABILITY, correction_evidence, reply_evidence, review_reply
from wxbot_framework.conversation.turns import (COLLECT_SECONDS, TurnFocus, exchange_intent, judge_turn,
                                                likely_incomplete, recent_exchange, turn_intents, turn_key)
from wxbot_framework.conversation.tool_router import REJECTED, JevToolRouter
from wxbot_framework.recent_images import RECENT_IMAGES
from wxbot_framework.integrations.llm import LLMConnectionError, LLMContentBlocked, LLMError, LLMFormatError
from wxbot_framework.conversation.members import BEIJING, SnapshotExpired
from wxbot_framework.messaging.mentions import addressed_elsewhere, at_source, bot_names, strip_at_labels, typed_at
from wxbot_framework.conversation.personality import personality_text
from wxbot_framework.conversation.utterance import (CHAT, DETAIL, QA, InterjectionGate, SpeakResult, classify_tier,
                                                    interjection_probe, speak)
from wxbot_framework.commands import IMAGE_COMMAND, command_text, is_control

GAME_FEATURE = "idiom_chain"
USED_SAMPLE = 10
# 提问含这些词时视为明确要求详细回答，使用 detail_max_tokens 预算。
DETAIL_HINTS = ("详细", "详解", "细说", "展开", "具体说说", "完整", "全文", "全篇", "原文", "一步一步", "逐步")
CATCH_UP = re.compile(r"补个?课|(?:最后|最终|后来).{0,4}(?:定|说好)|(?:刚才|刚刚|昨晚|昨天).{0,8}(?:聊|讨论)")
MEANING_QUESTION = re.compile(r"(?:什么|啥)意思[啊呀呢嘛吧]?[？?！!。]*$")
GROUP_ATTRIBUTION = re.compile(r"(?:群里|刚才|刚刚).{0,5}谁|谁.{0,6}(?:刚才|刚刚|说过|说的|讲过|发过|发的)")
VOICE_ACTION_WORDS = re.compile(r"唱|跑调|\b(?:sing|sang|sung)\b", re.IGNORECASE)
# 框架按已编译桥接声明的图片能力；没有时找不到图要如实说“收不到图片”，而不是让对方去引用。
IMAGE_CAPABILITY = "group.image.receive"
TOOL_FOLLOWUP_LIMIT = 64


def _arrival_time(received_at):
    queued = max(0.0, time.monotonic() - received_at)
    return datetime.now(timezone.utc) - timedelta(seconds=queued)


class TurnChanged(Exception):
    """同一人补了会改变答案的条件，尚未发送的回复重做。"""


class TurnNeedsReview(Exception):
    """打字节奏等待期间收到补充，退出发送锁后再判断。"""


def read_aliases(config):
    """配置里的额外称呼：群友手打 ``@有个ai`` 这类写法也要认。"""
    value = config.get("mention_aliases", [])
    if not isinstance(value, list) or not all(isinstance(name, str) and 0 < len(name) <= 24 for name in value):
        raise ValueError("mention_chat.mention_aliases 应为 1–24 字的字符串列表")
    return tuple(value)


def wants_detail(query):
    return any(hint in query for hint in DETAIL_HINTS)


def reply_seconds(query):
    lookup_hints = ("联网", "核实", "查证", "搜一下", "https://", "http://")
    return 120 if wants_detail(query) or any(word in query for word in lookup_hints) else 90


def _tool_followup(cache, key, received_at, window):
    saved = cache.get(key)
    return saved[1] if saved and 0 <= received_at - saved[0] <= window else None


def _remember_tool_followup(cache, key, received_at, value):
    cache[key] = (received_at, value)
    cache.move_to_end(key)
    if len(cache) > TOOL_FOLLOWUP_LIMIT:
        cache.popitem(last=False)


class Plugin:
    def __init__(self):
        # 同一群、同一发言者、相同提问的回复在生成或发送期间不重复生成。
        # key -> (首次提问到达时刻, 生成开始时刻, 生成结束时刻)；生成中时结束时刻为 None。
        self._pending = {}
        self._pending_lock = threading.Lock()
        self.aliases = ()  # on_load 按配置填；测试与热重载时缺省为空
        self.tools = None  # on_load 按配置填看图 / 联网阈值；None 表示不调用工具
        self._image_choices = OrderedDict()
        self._last_searches = OrderedDict()
        self._followup_lock = threading.Lock()
        self._slow = None
        self.cold = None
        self.voice_reply = VoiceReply()
        self.image_reply = ImageReply()
        self._wait = None
        self._status = None
        self._retry = OrderedDict()

    def _claim(self, key, arrived_at):
        """认领本次生成；仍在生成中，或本次到达时首次回复尚未结束，返回 False。

        分发线程串行处理消息，重复提问往往在首次回复结束后才被取出，
        所以不能只看“是否仍在生成”，要按消息实际到达时刻判断：
        到达时刻与结束时刻同为 time.monotonic()，到达不晚于首次结束即视为重复。
        """
        with self._pending_lock:
            previous = self._pending.get(key)
            if previous is not None:
                ended = previous[2]
                if ended is None or arrived_at <= ended:
                    return False
            self._pending[key] = (arrived_at, time.monotonic(), None)
            return True

    def _release(self, key, *, delivered):
        """成功后保留本轮起止时刻；失败则直接清理，便于用户重试同一提问。"""
        with self._pending_lock:
            record = self._pending.get(key)
            if record is None:
                return
            if not delivered:
                del self._pending[key]
                return
            self._pending[key] = (record[0], record[1], time.monotonic())

    def on_load(self, ctx):
        self.members = ctx.members
        self.room_state = getattr(ctx, "room_state", None)
        if self.room_state is not None:
            self.cold = ColdReply(self.members, self.room_state)
        self.aliases = read_aliases(ctx.config)
        self.tools = read_tool_options(ctx.config)
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。
        self._slow = SlowReplies(ctx.logger)
        self._status = StatusReplies(ctx.logger)
        self._wait = ReplyWait(self._status)
        self.image_reply.start(ctx, self._status)
        if "voice.send" in getattr(ctx, "capabilities", ()):
            self.voice_reply.start(ctx.logger)

    def has_pending_callbacks(self):
        return ((self._slow is not None and self._slow.busy()) or self.image_reply.busy() or
                (self._status is not None and self._status.busy()))

    def on_unload(self, ctx):
        if self._wait is not None:
            self._wait.close()
        self.image_reply.close()
        self.voice_reply.close()
        if self._slow is not None:
            self._slow.close()
        if self._status is not None:
            self._status.close()

    def _ensure_running(self):
        if self._slow is not None and self._slow.stopped.is_set():
            raise SnapshotExpired("艾特插件已停止")

    @contextmanager
    def _delivery_guard(self, message, guard, *args):
        with guard(*args):
            self._ensure_running()
            if self.cold is not None and self.cold.active(message):
                raise SnapshotExpired("发言者已被冷处理")
            yield

    def room_game(self, room, ctx, *, message=None):
        """本群正在进行的接龙进度；没有或读不出来时返回 None。"""
        if message is not None and "room_states" in message.data:
            state = message.data["room_states"].get(GAME_FEATURE)
        else:
            if self.room_state is None:
                return None
            try:
                state = self.room_state.get(room, GAME_FEATURE)
            except Exception as error:
                ctx.logger.warning("读取接龙进度失败：%s", error)
                return None
        if idiom_state.current_and_used(state) is None:
            return None
        idle = state.get("idle_seconds", 1800)
        if type(idle) is not int or not 60 <= idle <= 86400:
            return None
        age = idiom_state.updated_age(state)
        if age is None:
            return None
        return state if 0 <= age <= idle else None

    @staticmethod
    def is_move(text, state):
        """四字且首字等于当前接字，就是要交给接龙插件判定的答案。"""
        return idiom_state.is_move(text, state["current"])

    @staticmethod
    def game_context(state):
        """只读投影；字段解释见 plugins/_shared/idiom_state.py。"""
        return idiom_state.game_context(state, sample=USED_SAMPLE)

    def _bot_names(self, room):
        return bot_names(self.members, room, self.aliases)

    def _tools(self, ctx):
        try:
            jev = ctx.jev
        except Exception as error:
            ctx.logger.warning("Jev 不可用，只按明确说法看图或搜索：%s", type(error).__name__)
            jev = None
        return MentionTools(router=JevToolRouter(jev, **self.tools), magpie=lambda: ctx.magpie,
                            images=RECENT_IMAGES, image_supported=IMAGE_CAPABILITY in getattr(ctx, "capabilities", ()),
                            logger=ctx.logger, stopped=self._slow.stopped if self._slow is not None else None)

    def _run_tools(self, message, ctx, question, snapshot=None, *, resolved_quote=None, follow=False,
                   group_context=None, stop_at=None):
        """本轮艾特的看图 / 联网；没开启时返回空结果。Jev 读不出来时只按明确说法执行。"""
        if self.tools is None:
            return ToolOutcome()
        tools = self._tools(ctx)
        # 只艾特没写正文时，他上一条发言（如“这张图是啥意思？”）才是真正的问题。
        context_question = ""
        followup_topic = None
        now = _arrival_time(message.received_at)
        if not question and snapshot is not None:
            # 以这条艾特到达的墙钟时刻比较，扣掉它排队等待处理的时间。
            names = self._bot_names(message.chat_id)
            context_question = previous_question(
                snapshot.messages, now=now,
                is_mention=lambda text: bool(typed_at(text, names)))
        if snapshot is not None:
            followup_topic = public_search_topic(question or context_question, previous=snapshot.messages,
                                                 history=snapshot.history, now=now)
        key = (message.session_id, message.chat_id, message.sender_id)
        arrived = message.received_at
        with self._followup_lock:
            choices = _tool_followup(self._image_choices, key, arrived, IMAGE_WINDOW_SECONDS) or ()
            last_search, last_subject = _tool_followup(self._last_searches, key, arrived, SEARCH_RETRY_SECONDS) or ("", "")
        raw_quote = message.data.get("quote") or {}
        bot_quote = self.members.account in (raw_quote.get("fromusr"), raw_quote.get("chatusr"))
        focus = TurnFocus(question or context_question)
        if bot_quote and resolved_quote:
            focus = replace(focus, reference=resolved_quote["text"][:220])
        elif follow:
            previous = self.members.turns.continuation(message)
            if previous is not None:
                focus = replace(focus, anchor=previous.question[:180], reference=previous.text[:220])
        elif snapshot is not None and not raw_quote and question and (stop_at is None or time.monotonic() < stop_at):
            anchor, reference = recent_exchange(snapshot.history, now)
            intent = exchange_intent(question, reference) if anchor else ""
            if intent and self._turn_judge(ctx, intent, anchor, previous=reference, new=question):
                focus = replace(focus, anchor=anchor, reference=reference, clarifying=intent == "clarify")
        outcome = yield from tools.steps(question=question, room=message.chat_id, sender=message.sender_id,
                                         received_at=arrived, quote=message.data.get("quote"),
                                         image_choices=choices, previous_search=last_search, followup_topic=followup_topic,
                                         previous_subject=last_subject, focus=focus,
                                         group_context=group_context,
                                         search_date=now.astimezone(BEIJING).date().isoformat(), stop_at=stop_at)
        with self._followup_lock:
            if outcome.image_choices:
                _remember_tool_followup(self._image_choices, key, arrived, outcome.image_choices)
            elif choices and any(result.image_number for result in outcome.results):
                self._image_choices.pop(key, None)
            if outcome.search_query:
                _remember_tool_followup(self._last_searches, key, arrived,
                                        (outcome.search_query, outcome.search_subject or outcome.search_query))
        return outcome

    def _clean_query(self, message, source):
        text = strip_at_labels(message.data.get("retry_question", message.text))
        if source == "typed":
            label = typed_at(text, self._bot_names(message.chat_id))
            if label:
                text = text.replace("@" + label, "")
        return text.strip()

    @staticmethod
    def _turn_judge(ctx, intent, question, **context):
        try:
            jev = ctx.jev
        except ValueError:
            return False
        return jev is not None and judge_turn(jev, intent, question, **context)

    def _meaning_reference(self, message, ctx, question, snapshot, quote, *, quote_is_bot):
        raw_quote = message.data.get("quote")
        if raw_quote:
            ref_type = raw_quote.get("ref_type")
            if quote_is_bot and quote and (ref_type in (None, "1") or
                                          (ref_type == "49" and raw_quote.get("ref_app_type") == "57")):
                return quote["text"]
            return ""
        anchor, reference = recent_exchange(snapshot.history, _arrival_time(message.received_at))
        if reference and self._turn_judge(ctx, "refer", anchor, previous=reference, new=question):
            return reference
        return ""

    def _advance_turn(self, turns, turn, ctx, *, deadline=None):
        changed = False
        for incoming in turns.additions(turn, deadline=deadline):
            source = at_source(incoming, self.members, self.aliases)
            quote = incoming.data.get("quote")
            if quote or (source is None and (incoming.data.get("at_members") or incoming.text.startswith("@"))):
                continue
            text = self._clean_query(incoming, source)
            if (source in ("structural", "typed") and text and not incoming.is_self
                    and "group.image.send" in getattr(ctx, "capabilities", ())):
                budget = CURRENT.get()
                if budget and budget.deadline - budget.clock() < FORMAT_SECONDS:
                    continue  # 留给新消息自己的预算，避免旧轮收尾时把生图要求并掉。
                if self.image_reply.intent(incoming, ctx, text):
                    continue
            if source and (not text or text == turn.question):
                turns.merge(incoming)
            elif text and self._turn_judge(ctx, "merge", turn.question, new=text):
                turns.merge(incoming)
                turn.question = "\n".join(filter(None, (turn.question, text)))
                previous_quote = turn.message.data.get("quote")
                turn.message = (replace(incoming, data={**incoming.data, "quote": previous_quote})
                                if previous_quote else incoming)
                changed = True
        return changed

    def on_message(self, message, ctx):
        # 图片事件（type 3）由原生服务记入 RECENT_IMAGES；这里处理艾特与定向续问。
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                "WXBOT_AT_CHAT_PROBE" in message.text or
                message.data.get("bot_generated") or message.data.get("profile_confirmation")):
            return
        if is_control(message.text):
            parts = command_text(message.text).split(maxsplit=1)
            args = parts[1] if len(parts) == 2 else ""
            if parts[0] == IMAGE_COMMAND:
                if not self.image_reply.followup(message, ctx, args, addressed=True, cold=self.cold):
                    self.image_reply.request(message, ctx, args, cold=self.cold)
                return
            if parts[0] == VOICE_COMMAND:
                self.voice_reply.command(message, ctx, args)
            elif parts[0] == VOICE_REPLY_COMMAND and not message.is_self:
                self.voice_reply.reply_command(message, ctx, args)
            elif parts[0] == COLD_COMMAND and self.cold is not None and not message.is_self:
                self.cold.command(message, ctx, args)
            if parts[0] != VOICE_READ_COMMAND or message.is_self:
                return
            resume = lambda: self._read_voice_command(message, ctx, args)
        else:
            turns = getattr(self.members, "turns", None) if message.dispatch_sequence else None
            resume = lambda: self._process_message(message, ctx, turns)
        key = turn_key(message)
        waiting = None
        if not is_control(message.text) and at_source(message, self.members, self.aliases):
            if self._wait is not None:
                try:
                    waiting = self._wait.accept(message, ctx, reply_seconds(message.text))
                except SnapshotExpired:
                    return
                original = resume
                resume = lambda: self._wait.wrap(original(), waiting)
        def queue_full():
            if waiting is not None:
                self._wait.finish(waiting, "我前面排了太多条消息，这条还没轮到处理。")

        if self._slow is None:
            for _ in resume():
                pass
        elif not self._slow.queue_if_active(key, resume, on_full=queue_full):
            turn = resume()
            if not self._slow.handoff(key, turn):
                queue_full()
                turn.close()

    def _read_voice_command(self, message, ctx, text):
        self._ensure_running()
        if not text:
            ctx.reply(message, "用法：/语音 要念的原文，无需艾特。普通成员发送后判断内容；不当时全群普通成员停用一小时，管理员豁免。")
            return
        if "voice.send" not in getattr(ctx, "capabilities", ()):
            ctx.reply(message, "当前微信连接不支持发送语音。")
            return
        paused = self.voice_reply.read_pause_seconds(message, ctx)
        if paused:
            ctx.reply(message, f"本群普通成员的 /语音 暂停中，约 {(paused + 59) // 60} 分钟后恢复。管理员仍可使用。")
            return
        personality = ctx.personality(message)
        sent_parts = []

        @contextmanager
        def guard():
            with ctx.personality_guard(personality):
                self._ensure_running()
                if self.voice_reply.read_pause_seconds(message, ctx):
                    raise SnapshotExpired("本群 /语音 已暂停")
                yield

        try:
            remaining = yield from self.voice_reply.deliver(
                message, ctx, text, voice=personality.voice, guard=guard, split=True, on_sent=sent_parts.append)
            if remaining is not None:
                result = speak(remaining, tier=QA, byte_budget=1023,
                               send_at=lambda part: ctx.reply(message, part, delivery_guard=guard))
                if result.failed:
                    raise RuntimeError("原文语音回退文字发送未成功")
        except SnapshotExpired as error:
            ctx.logger.info("原文语音已取消：%s", error)
        finally:
            if sent_parts:
                self.voice_reply.review_read(message, ctx, " ".join(sent_parts))

    def _process_message(self, message, ctx, turns):
        # 判断、模型生成和工具等待都在成员队列内，不占插件收消息的线程。
        self._ensure_running()
        active_budget = CURRENT.get()
        if active_budget is not None:
            with active_budget.lock:
                active_budget.stage = "理解问题"
        if turns is not None and turns.merged(message):
            return
        source = at_source(message, self.members, self.aliases)
        clean_query = self._clean_query(message, source)
        game = self.room_game(message.chat_id, ctx, message=message)
        if game and self.is_move(clean_query, game):
            return
        answer = self.image_reply.format_answer(message, ctx, clean_query, source)
        if answer:
            message, original, mode = answer
            if mode == "create":
                if turns is not None:
                    turns.claim(message)
                self.image_reply.request(message, ctx, original, mode=mode, cold=self.cold)
                return
            clean_query = original + "\n请写成文字。"
            source = "format"
        image_target = not message.is_self and source in ("structural", "typed")
        image_intent = self.image_reply.intent(message, ctx, clean_query) if image_target else None
        if image_target and image_intent is None and self.image_reply.status(message, ctx, clean_query):
            return
        if not answer and self.image_reply.followup(message, ctx, clean_query, addressed=image_target, cold=self.cold):
            if turns is not None:
                turns.claim(message)
            return
        if image_intent is not None:
            if turns is not None:
                turns.claim(message)
            self.image_reply.request(message, ctx, clean_query, mode=image_intent.mode,
                                     previous_prompt=image_intent.previous_prompt, cold=self.cold)
            return
        if (not answer and self._wait is not None and (source or not addressed_elsewhere(message)) and
                self._wait.followup(message, ctx, clean_query, addressed=source is not None)):
            if turns is not None:
                turns.claim(message)
            return
        depth = 0
        if source is None:
            if turns is None or not clean_query or turns.active(message) or addressed_elsewhere(message):
                return
            previous = turns.continuation(message)
            if previous is None or not self._turn_judge(ctx, "follow", previous.question,
                    previous="\n".join(filter(None, (previous.text, previous.voice_action))), new=clean_query):
                return
            source, depth = "follow", previous.depth + 1
        elif turns is not None and not clean_query and not message.data.get("quote") and turns.active(message):
            turns.merge(message)
            return
        elif source is not None:
            key = turn_key(message)
            with self._followup_lock:
                previous = _tool_followup(self._retry, key, message.received_at, 300)
                retry = clean_query in ("重试", "再试一次") and previous is not None
                if not retry:
                    _remember_tool_followup(self._retry, key, message.received_at, message)
            if retry:
                message = replace(message, data={**message.data, "retry_question": previous.text,
                                                "quote": previous.data.get("quote")})
                clean_query = self._clean_query(message, source)
        if turns is not None:
            turns.claim(message)
        if CURRENT.get() is None and self._wait is not None:
            try:
                waiting = self._wait.accept(message, ctx, reply_seconds(clean_query))
            except SnapshotExpired:
                return
            yield from self._wait.wrap(self._reply_turn(message, ctx, clean_query, game, turns=turns,
                                                       source=source, depth=depth), waiting)
            return
        yield from self._reply_turn(message, ctx, clean_query, game, turns=turns, source=source, depth=depth)

    def _reply_turn(self, message, ctx, clean_query, game, *, turns=None, source=None, depth=0):
        if self.cold is not None and self.cold.active(message):
            reply = self.cold.reply(message, ctx, clean_query)
            if reply:
                if ("voice.send" in getattr(ctx, "capabilities", ())
                        and self.voice_reply.fixed(message, self.room_state)):
                    personality = ctx.personality(message)
                    @contextmanager
                    def guard():
                        with ctx.personality_guard(personality):
                            self._ensure_running()
                            if not self.cold.active(message):
                                raise SnapshotExpired("冷处理已解除")
                            yield
                    try:
                        remaining = yield from self.voice_reply.deliver(
                            message, ctx, reply, voice=personality.voice, guard=guard, split=True, fixed=True)
                        if remaining is not None:
                            ctx.reply_at(message, remaining, delivery_guard=guard)
                    except SnapshotExpired:
                        pass
                else:
                    ctx.reply_at(message, reply)
            return
        if turns is None:
            yield from self._reply_once(message, ctx, clean_query, game)
            return
        if turns.merged(message):
            return
        previous = turns.continuation(message)
        turn = turns.open(message, clean_query, depth)
        active_budget = CURRENT.get()
        try:
            self._advance_turn(turns, turn, ctx)
            deadline = message.received_at + COLLECT_SECONDS
            if (source != "follow" and not message.data.get("quote") and time.monotonic() < deadline
                    and likely_incomplete(turn.question) and self._turn_judge(ctx, "collect", turn.question)):
                yield  # 等同一人把话说完时，不占其他群友的正常回复线程。
                while time.monotonic() < deadline and turns.active(message) is turn:
                    if self._advance_turn(turns, turn, ctx, deadline=deadline):
                        break

            def before_send():
                if not turns.has_addition(turn):
                    return
                if active_budget is not None:
                    with active_budget.lock:
                        active_budget.reviewing_additions = True
                try:
                    if self._advance_turn(turns, turn, ctx):
                        if active_budget is not None:
                            active_budget.revise()
                        raise TurnChanged
                finally:
                    if active_budget is not None:
                        with active_budget.lock:
                            active_budget.reviewing_additions = False

            while True:
                if source == "follow" or turn.message is not message:
                    ctx.logger.info("群聊接话：来源=%s 目标=%s", source, turn.message.dispatch_sequence)
                try:
                    yield from self._reply_once(turn.message, ctx, turn.question, game, before_send=before_send,
                                                needs_review=lambda: turns.has_addition(turn),
                                                sent=lambda text, **facts: turns.sent(turn, text, **facts),
                                                fallback_sent=lambda text: turns.sent(turn, text, allow_closed=True),
                                                previous=previous, plain=source == "follow")
                except TurnChanged:
                    if turn.replanned:
                        ctx.logger.info("群聊接话：按最新补充另起一轮 目标=%s", turn.message.dispatch_sequence)
                    turn.replanned = not turn.replanned
                    continue
                return
        finally:
            turns.close(turn)

    def _reply_once(self, message, ctx, clean_query, game, *, before_send=None, needs_review=None, sent=None, fallback_sent=None,
                    previous=None, plain=False):
        if self._slow is not None and self._slow.stopped.is_set():
            return
        if self.cold is not None and self.cold.active(message):
            return  # 本轮排队期间设置了冷处理。
        personality = ctx.personality(message)
        guard = lambda: self._delivery_guard(message, ctx.personality_guard, personality)
        key = (message.session_id, message.chat_id, message.sender_id, clean_query)
        if not self._claim(key, message.received_at):
            ctx.logger.info("艾特对话跳过：相同提问仍在回复中 room=%s", message.chat_id)
            return
        result = SpeakResult()
        active_budget = CURRENT.get()
        revision = active_budget.revision if active_budget is not None else None
        prepare_until = active_budget.prepare_until if active_budget is not None else None
        answer_until = active_budget.answer_until if active_budget is not None else None

        def work(stage, until):
            return active_budget.work(stage, until) if active_budget is not None else nullcontext()

        def can_prepare():
            return prepare_until is None or active_budget.clock() < prepare_until

        def dispatch(send):
            while True:
                if before_send is not None:
                    before_send()
                try:
                    return send()
                except TurnNeedsReview:
                    continue

        try:
            catch_up = CATCH_UP.search(clean_query)
            snapshot = (self.members.conversation_snapshot(message, group_limit=100, group_only=True)
                        if catch_up else self.members.conversation_snapshot(message))
            @contextmanager
            def guard():
                with self._delivery_guard(message, ctx.response_guard, snapshot, personality):
                    if active_budget is not None:
                        active_budget.remaining(1)
                    if needs_review is not None and needs_review():
                        raise TurnNeedsReview
                    if active_budget is None:
                        yield
                    else:
                        with active_budget.delivery(revision):
                            yield
            raw_quote = message.data.get("quote") or {}
            routing_snapshot = snapshot
            quote_is_bot = self.members.account in (raw_quote.get("fromusr"), raw_quote.get("chatusr"))
            quote_hint = {"text": raw_quote.get("text") or "", "作者": "机器人" if quote_is_bot else "群友"} if raw_quote else None
            display = self.members.at_display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            budget = 1023 - len(("@" + display + "\u2005\n").encode("utf-8"))
            voice_supported = "voice.send" in getattr(ctx, "capabilities", ())
            try:
                judge = getattr(ctx, "jev", None)
            except ValueError:
                judge = None
            with work("理解问题", prepare_until):
                intents = turn_intents(judge, clean_query, quote=quote_hint, previous=previous)
            if "correction" in intents and quote_is_bot:
                quote, missing_quote = None, ""
            else:
                quote, missing_quote = resolve_reply_quote(raw_quote, lambda mid: self.members.context_text(message.chat_id, mid))
            offer_reading = "sing" in intents
            read_accepted = "read" in intents and not offer_reading and voice_supported
            reading_offer = clean_query if offer_reading and voice_supported else ""
            voice_action = ""
            self_facts = {}
            if offer_reading or read_accepted or "correction" in intents or (previous and previous.voice_action and not clean_query):
                self_facts["语音能力"] = VOICE_CAPABILITY if voice_supported else "当前连接只可发文字，不能唱歌或发语音。"
            if offer_reading:
                self_facts["本次动作"] = ("只用一句话说明不会唱歌，可以朗读，等对方同意。现在还未朗读。"
                                         if voice_supported else "简短说明当前发不了语音。")
            elif read_accepted:
                self_facts["本次动作"] = "群友已同意朗读。直接输出适合朗读的内容，勿声称已读完或唱过。"
                self_facts["获准朗读的原请求"] = previous.reading_offer
            if previous and previous.voice_action and not clean_query:
                self_facts["同一发言者上一轮的语音结果"] = previous.voice_action
            if "correction" in intents:
                with work("查群聊资料", prepare_until):
                    originals = correction_evidence(judge if can_prepare() else None, self.members, snapshot, message,
                                                    clean_query, quote=quote_hint if quote_is_bot else None,
                                                    quote_id=raw_quote.get("svrid", "") if quote_is_bot else "")
                own_lines = [row for row in originals if row["发言人"] == "机器人"]
                self_facts["原话核对"] = "找到相关聊天原文" if originals else "本群最近14天没有找到对应的聊天原话"
                self_facts["机器人原话"] = own_lines
                self_facts["群友原话"] = [row for row in originals if row["发言人"] != "机器人"]
                snapshot = replace(snapshot, history=tuple(row for row in snapshot.history
                                   if not (row.get("bot_generated") or row.get("is_self"))))
                if previous and previous.voice_action and (not raw_quote or any(r["形式"] == "朗读语音" for r in own_lines)):
                    self_facts["同一发言者上一轮的语音结果"] = previous.voice_action

            corrections = getattr(ctx.knowledge, "corrections", None)
            matches = CorrectionMatches()

            def remember_fact_use():
                if corrections is not None and matches.facts:
                    corrections.record_use(snapshot.room, snapshot.anchor_id, matches.facts)

            def send_part(part, send_guard=guard, *, use_at=False):
                if use_at and not plain:
                    delivered = dispatch(lambda: ctx.reply_at(message, part, display_name=display, delivery_guard=send_guard))
                else:
                    delivered = dispatch(lambda: ctx.reply(message, part, delivery_guard=send_guard))
                if delivered and sent is not None:
                    sent(part, reading_offer=reading_offer, voice_action=voice_action)
                if delivered and active_budget is not None:
                    active_budget.submitted()
                if delivered:
                    remember_fact_use()
                return delivered

            def send_at(part, send_guard=guard):
                return send_part(part, send_guard, use_at=True)

            def remember_draft(reply):
                if active_budget is not None and not (offer_reading or read_accepted):
                    with active_budget.lock:
                        if not active_budget.closed and active_budget.revision == revision:
                            active_budget.ready = ReplyDraft(revision, reply, needs_review, fallback_sent)

            fixed_voice = voice_supported and self.voice_reply.fixed(message, self.room_state)
            turn_voice = False

            def respond(reply, tier, *, preference=False):
                nonlocal voice_action
                if self.voice_reply.text_only(message, self.room_state) and not turn_voice:
                    preference = False
                use_voice = not offer_reading and (read_accepted or fixed_voice or (voice_supported and self.voice_reply.reserve(
                    message, reply, preference, automatic=tier == CHAT)))
                if use_voice:
                    if active_budget is not None:
                        with active_budget.lock:
                            active_budget.stage = "合成语音"
                    def record_voice(part):
                        result.sent.append(part)
                        remember_fact_use()
                        if active_budget is not None:
                            active_budget.submitted()
                        if sent is not None:
                            sent(part, voice_action="已提交朗读语音")
                    def record_outcome(outcome):
                        nonlocal voice_action
                        if outcome == "unknown":
                            if sent is not None:
                                sent("", voice_action="朗读语音提交结果未确认，已停止补发")
                        else:
                            voice_action = ("语音合成失败，已改发文字" if outcome == "failed"
                                            else "固定语音间隔内，已改发文字")
                            if result.sent:
                                voice_action = "部分朗读已提交，剩余内容因合成失败改发文字"
                    remaining = yield from self.voice_reply.deliver(
                        message, ctx, reply, voice=personality.voice, guard=guard, split=fixed_voice or read_accepted,
                        dispatch=dispatch, on_sent=record_voice, on_outcome=record_outcome, fixed=fixed_voice,
                        automatic=not (fixed_voice or read_accepted or turn_voice or preference))
                    if remaining is None:
                        return
                    reply = remaining
                if active_budget is not None:
                    with active_budget.lock:
                        active_budget.stage = "准备发送文字"
                gate = InterjectionGate(interjection_probe(self.members, message.chat_id)) if tier == CHAT else None
                send_guard = gate.wrap(guard) if gate is not None else guard
                speak(reply, tier=tier, byte_budget=budget, result=result,
                      send_at=lambda part: send_at(part, send_guard),
                      send_plain=lambda part: send_part(part, send_guard), interjected=gate)
                if result.failed:
                    raise RuntimeError("原生艾特发送未成功")
                if result.interrupted:
                    ctx.logger.info("闲聊拆分发送中有人插话，丢弃剩余 %d 条：room=%s", result.dropped, message.chat_id)

            investigation = None
            fresh = False
            quoted_image = raw_quote.get("ref_type") in ("3", "47")
            learned = False
            facts = corrections.list(snapshot.room) if corrections is not None and not (offer_reading or read_accepted) else []
            chain = ()
            quoted_text = ""
            recent = [{"消息编号": row["id"], "发言人": row.get("speaker_name"), "内容": row["text"][:300]}
                      for row in routing_snapshot.history[-8:]] if facts else []
            used = ()

            def find_corrections(found, more=()):
                if not found:
                    return CorrectionMatches()
                with work("查已核实的纠错", prepare_until):
                    return route_corrections(found, clean_query, judge if can_prepare() else None,
                                             quote=quoted_text, recent=(*recent, *more), used=used,
                                             now=self.members.now(), logger=ctx.logger, anchor_id=snapshot.anchor_id)

            if facts:
                preview = strip_at_labels(raw_quote.get("text") or "").strip()
                native_id = raw_quote.get("svrid") or ""
                if native_id or preview:
                    chain = self.members.reference_chain(routing_snapshot, native_id, preview)
                quoted_text = "\n".join(row["text"][:350] for row in chain) or preview
                used = corrections.used(snapshot.room, tuple(row["id"] for row in chain))
                if previous and previous.question:
                    recent.append({"发言人": "同一发言者上一轮", "内容": previous.question[:300]})
                if not quoted_image:
                    matches = find_corrections(facts)
                if matches.ambiguous:
                    yield from respond(matches.clarification(), QA)
                    return
            try:
                planning_model = getattr(ctx, "llm", None)
            except ValueError:
                planning_model = None
            if (callable(getattr(planning_model, "complete_json", None))
                    and not (offer_reading or read_accepted or missing_quote or quoted_image) and can_prepare()):
                target_quote = quote or quote_hint
                if "correction" in intents and (quote_is_bot or (not target_quote and originals)):
                    target_quote = {"已核对的聊天原话": originals} if originals else None
                with work("理解问题", prepare_until):
                    fresh, needs_local, *signals = material_needs(judge, clean_query, snapshot, target_quote, previous)
                correcting_public = bool(signals and signals[0])
                fresh = bool(matches.refresh) or (fresh and (not matches.answers or correcting_public))
                learning = corrections is not None and (correcting_public or "correction" in intents or matches.refresh)
                has_link = "https://" in message.text or "http://" in message.text

                def verify_correction(sources):
                    nonlocal facts, matches
                    if active_budget is not None:
                        with active_budget.lock:
                            active_budget.llm_limit = 6
                    with work("核对纠错来源", prepare_until):
                        ids = capture_corrections(planning_model, judge, ctx, message, snapshot, clean_query, target_quote,
                                                  sources, matches,
                                                  guard=lambda: ctx.response_guard(snapshot, personality), store=corrections)
                    if not ids:
                        return False
                    facts = corrections.list(snapshot.room)
                    matches = find_corrections(facts)
                    return any(row.id in ids for row in matches.answers)

                if learning and (has_link or "correction" in intents) and can_prepare():
                    yield
                    learned = verify_correction(())
                    if learned:
                        fresh = False
                gather_local = needs_local and "correction" not in intents and classify_tier(clean_query) != CHAT
                if not learned and can_prepare() and (fresh or (gather_local and not matches.answers)):
                    if fresh and active_budget is not None:
                        with active_budget.lock:
                            active_budget.llm_limit = max(5, active_budget.llm_limit)
                    lookup = self._tools(ctx).search if fresh and self.tools is not None else None
                    try:
                        yield  # 与原有联网流程一样，让同群其他人的任务先运行。
                        with work("联网查资料" if fresh else "查群聊资料", prepare_until):
                            investigation = gather_answer_material(
                                planning_model, self.members, ctx.knowledge, message, snapshot, clean_query, target_quote,
                                current=fresh, search=lookup, guard=lambda: ctx.response_guard(snapshot, personality),
                                logger=ctx.logger)
                    except LLMContentBlocked:
                        raise
                    except (LLMError, TimeoutError) as error:
                        ctx.logger.info("群聊取材未完成：%s", type(error).__name__)
                    if investigation is not None and facts and not matches.facts and not matches.refresh:
                        more = [{"消息编号": item.get("消息编号"), "发言人": item.get("发言人"),
                                 "内容": item["内容"][:300]} for item in investigation.local
                                if item.get("消息编号")]
                        if more:
                            matches = find_corrections(facts, more)
                if learning and not learned and can_prepare():
                    sources = investigation.sources if investigation else ()
                    has_search_link = any(row.get("类别") == "公开搜索结果" and "http" in row["内容"] for row in sources)
                    history_ids = {row["id"] for row in snapshot.history[-10:]}
                    has_new_local = "correction" in intents and any(
                        item.get("类别") == "群友原话" and item.get("消息编号") not in history_ids for item in sources)
                    if has_search_link or has_new_local:
                        learned = verify_correction(sources)
                if fresh and not (investigation and investigation.public):
                    if matches.answers and not matches.refresh:
                        yield from respond("；".join(row.statement.rstrip("。") for row in matches.answers) + "。", QA)
                    elif matches.refresh:
                        when = matches.refresh[0].observed_on
                        yield from respond(f"这条现状目前只核实到 {when}，最新情况还没查到。", QA)
                    else:
                        yield from respond("这条近期消息我还没查到能确认的出处。", QA)
                    return
            if matches.ambiguous:
                yield from respond(matches.clarification(), QA)
                return
            if catch_up and not missing_quote and not self_facts and investigation is None and not matches.facts:
                rows = snapshot.group_messages
                with work("写回复", answer_until):
                    reply = Dialogue(ctx.llm_for(message, personality=personality, for_reply=True),
                                     display_timezone=ctx.display_timezone).catch_up(rows, clean_query, quote=quote)
                self._ensure_running()
                remember_draft(reply)
                yield from respond(reply, QA)
                ctx.logger.info("群聊补课完成：记录=%d", len(rows))
                return

            detail = wants_detail(clean_query)
            tools = build_default_tools(ctx.knowledge, self.members)
            if investigation is not None:
                context = MentionContext([], tools.call("speaker_style", room=message.chat_id, wxid=message.sender_id),
                    tools.call("speaker_profile", room=message.chat_id, wxid=message.sender_id), [],
                    [] if fresh else tools.call("speech_examples", room=message.chat_id))
            else:
                context = prefetch_mention_context(tools, message.chat_id, message.sender_id, clean_query)
            reviewer = self._tools(ctx).router.jev if self.tools is not None else None
            if self_facts:
                reviewer = judge
            RECENT_IMAGES.restore_sent(self.members, message.chat_id, quote=message.data.get("quote"),
                                       source_id=self.members.message_id(message))
            options = {"detail": True} if detail else {"examples": context.examples}
            if self.voice_reply.text_only(message, self.room_state):
                options["turn_context"] = ("本人持续文字偏好已保存。" +
                    ("管理员固定语音仍生效，须说明该冲突，不能承诺本轮只发文字。" if fixed_voice else "后续默认文字回复。"))
            if offer_reading or read_accepted:
                outcome = ToolOutcome()
            elif fresh and investigation is not None:
                outcome = ToolOutcome(results=[ToolResult("search", True, text=item["内容"])
                                               for item in investigation.public])
            elif missing_quote:
                outcome = ToolOutcome(ask=missing_quote)
            elif matches.answers and not quoted_image:
                outcome = ToolOutcome()
            else:
                if active_budget is not None and quoted_image:
                    with active_budget.lock:
                        active_budget.stage = "看图"
                outcome = yield from self._run_tools(message, ctx, clean_query, snapshot, resolved_quote=quote, follow=plain,
                                                     group_context=context.search_scope(snapshot.history), stop_at=prepare_until)
            material = []
            if can_prepare():
                with work("查群聊资料", prepare_until):
                    if investigation is None and not quoted_image and "correction" not in intents and not matches.answers:
                        material = person_context(self.members, ctx.knowledge, reviewer, message.chat_id, clean_query,
                                                  history=snapshot.history, before_id=self.members.message_id(message),
                                                  quote=quote, sender_id=message.sender_id)
                    turn_voice = self.voice_reply.update_preference(message, ctx, clean_query, reviewer)
            context = replace(context, person_messages=material)
            self._ensure_running()
            if before_send is not None:
                before_send()
            if outcome.used:
                options["tool_results"] = outcome.payload()
            if learned:
                options["turn_context"] = "\n".join(filter(None, (options.get("turn_context"),
                    "本轮已核实纠正。适用时简洁交代更正和出处，不清楚的动机或后续省略。")))
            if outcome.context or outcome.ask:
                options["turn_context"] = "\n".join(filter(None, (options.get("turn_context"), outcome.context, outcome.ask)))
            if investigation is not None and investigation.local:
                options["turn_context"] = "\n".join(filter(None, (options.get("turn_context"),
                    "本轮已找到的群聊资料（引用时看清发言人、时间和类别）：" +
                    json.dumps(investigation.local, ensure_ascii=False))))
            if (not (self_facts or outcome.used or outcome.context or outcome.ask) and MEANING_QUESTION.search(clean_query)
                    and (quote or can_prepare())):
                with work("理解这句在接什么", prepare_until):
                    reference = self._meaning_reference(message, ctx, clean_query, snapshot, quote, quote_is_bot=quote_is_bot)
                if reference:
                    explanation = (
                        f"本轮要解释的原话由你自己说出：{reference}\n"
                        "直接用一句口语解释这段话想表达的意思。只谈你的本意，省去背景复述和对其他发言者言行、动机的归因。"
                    )
                    options["turn_context"] = "\n".join(filter(None, (options.get("turn_context"), explanation)))
            can_voice = not offer_reading and (read_accepted or fixed_voice or (not detail and not outcome.used and voice_supported))
            if can_voice:
                options["system"] = SYSTEM + VOICE_PROMPT
                if fixed_voice:
                    options["system"] += FIXED_VOICE_PROMPT
            if fresh:
                options["system"] = options.get("system", SYSTEM) + (
                    "\n本轮核实的是近期可能变动的公开状态。以本轮可核对的公开资料为准，"
                    "直说结果并写出处和事件时间。群友转述或自己之前的话不能用作公开事实的证据。")
            correction_material = [{**fact.material(), "用途": use}
                                   for use, rows in (("直接答案", matches.answers), ("承接前提", matches.premises))
                                   for fact in rows]

            correction_reply = ""
            if "correction" in intents and originals and not fresh:
                matched = [rows[0] for rows in (self_facts["群友原话"], own_lines) if rows]
                correction_reply = "我翻到的原话是，" + "。".join(
                    f"{'你' if row['本人'] else '我' if row['发言人'] == '机器人' else row['发言人']}说过「{row['原话'][:140]}」"
                    for row in matched) + "。"
                remember_draft(correction_reply)

            def generate(*, guidance="", revision=None):
                self._ensure_running()
                prompt_options = options if not guidance else {
                    **options, "system": options.get("system", SYSTEM) + "\n" + guidance,
                }
                with work("写回复", answer_until):
                    text = personality_text(Dialogue(ctx.llm_for(message, personality=personality, for_reply=True)).reply(
                        snapshot, knowledge=context.knowledge, style=context.style,
                        speaker_profile=context.speaker_profile, people=context.people, person_messages=context.person_messages,
                        room_game=self.game_context(game) if game else None, quote=quote,
                        question=clean_query, revision=revision, self_facts=self_facts,
                        corrections=correction_material, **prompt_options),
                        personality.name)
                text, preference = voice_reply_format(text) if can_voice else (text, None)
                if not text:
                    raise LLMFormatError("大模型对话结果格式不正确")
                return text, preference

            reply, voice_preference = generate()
            self._ensure_running()
            review_facts = self_facts
            if not review_facts and VOICE_ACTION_WORDS.search(reply):
                review_facts = {"语音能力": VOICE_CAPABILITY}
            checked_search = any(item.tool == "search" and item.ok for item in outcome.results)
            group_attribution = bool(GROUP_ATTRIBUTION.search(clean_query))
            for attempt in range(2):
                verified = {row["编号"]: row for row in correction_material}
                if facts and not quoted_image and not fresh:
                    for group in correction_candidates(facts, reply):
                        if group["name"] or group["exact"]:
                            fact = next((row for row in group["records"] if row.status == "active"), None)
                            if fact is not None:
                                verified.setdefault(fact.id, fact.material())
                if not (fresh or review_facts or checked_search or group_attribution or verified):
                    break
                with work("核对回复", active_budget.deadline if active_budget else None):
                    if fresh:
                        accepted = supports_current_answer(judge, clean_query, reply, investigation.public_text,
                                                           now=self.members.now())
                        contradicts = False
                    else:
                        evidence = reply_evidence(snapshot, context, quote=quote, quote_is_bot=quote_is_bot,
                            tools=outcome.payload(), room_game=self.game_context(game) if game else None,
                            turn_context=outcome.context, self_facts=review_facts)
                        if verified:
                            evidence["已核实纠错"] = list(verified.values())[:9]
                            evidence["本轮日期"] = self.members.now().date().isoformat()
                        decision = review_reply(judge if review_facts or verified else reviewer, clean_query, reply, evidence)
                        accepted = decision.status != REJECTED
                        contradicts = (decision.correction or 0) >= 0.7
                        ctx.logger.info("回复依据核对：status=%s local=%s public=%s own=%s correction=%s ids=%s error=%s",
                                        decision.status, decision.local, decision.public, decision.own,
                                        decision.correction, list(verified), decision.error)
                if accepted:
                    break
                if attempt == 0 and (active_budget is None or active_budget.clock() + 8 < answer_until):
                    try:
                        reply, voice_preference = generate(revision=reply, guidance=(
                            "按已有资料答当前问题，删掉没有依据的细节。纠错时区分群友和机器人各自说过的话，"
                            "公开近况注明出处和时间，没查到的部分直说。"
                            + ("用已核实纠错对齐同一对象、关系和时间，删改明确冲突的断言。" if verified else "")))
                        continue
                    except LLMContentBlocked:
                        raise
                    except (LLMError, ReplyExpired):
                        pass
                if contradicts and matches.answers:
                    reply = "；".join(row.statement.rstrip("。") for row in matches.answers) + "。"
                elif correction_reply:
                    reply = correction_reply
                elif fresh or checked_search or verified:
                    reply = "这条消息我还没核实清楚，先不下结论。"
                elif group_attribution or "correction" in intents:
                    reply = "我刚才没对上这句是谁说的，现有记录里还没找到。"
                else:
                    reply = VOICE_CAPABILITY
                voice_preference = False
                break
            # 用了工具的回复按问答单条发出；带网址的消息容易触发微信风控，模型照抄了网址也在这里去掉。
            if outcome.used or correction_material:
                reply = strip_links(reply) or reply
            tier = QA if self_facts else ((DETAIL if detail else QA) if outcome.used or outcome.ask or material
                                         else classify_tier(clean_query, detail=detail))
            remember_draft(reply)
            yield from respond(reply, tier, preference=voice_preference if can_voice else False)
            ctx.logger.info("艾特对话完成：room=%s persona=%s context=%d", message.chat_id,
                            personality.name, len(snapshot.messages))
        except SnapshotExpired as error:
            ctx.logger.info("艾特对话已取消：%s", error)
        except LLMContentBlocked:
            ctx.logger.info("艾特对话未生成正文：内容安全过滤拦截")
            active_budget = CURRENT.get()
            if self._wait is not None and active_budget is not None:
                self._wait.finish(active_budget, "本轮请求被模型的内容安全过滤拦截，回答未完成", blocked=True)
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
            active_budget = CURRENT.get()
            if self._wait is not None and active_budget is not None:
                if isinstance(error, LLMConnectionError):
                    reason = timeout_reason(active_budget.stage)
                elif isinstance(error, LLMFormatError):
                    reason = "刚才模型回过来的内容里，我没能读出一段正文。"
                else:
                    reason = "刚才写回复的时候模型接口报错了，我没收到能发的文字。"
                self._wait.finish(active_budget, reason)
        finally:
            # 已发出至少一条（含守卫或异常发生在首条之后）就保留去重记录，避免排队中的同一提问重发。
            self._release(key, delivered=bool(result.sent))
