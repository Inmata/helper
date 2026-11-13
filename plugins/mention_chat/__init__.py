"""真正艾特机器人后，用本群人格回复当前发言者。"""
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .._shared import idiom_state
from .cold_reply import COMMAND as COLD_COMMAND, ColdReply
from .slow_replies import SlowReplies
from .voice_reply import (COMMAND as VOICE_COMMAND, READ_COMMAND as VOICE_READ_COMMAND,
                          REPLY_COMMAND as VOICE_REPLY_COMMAND, FIXED_VOICE_PROMPT,
                          VOICE_PROMPT, VoiceReply, voice_reply_format)

from wxbot_framework.conversation.dialogue import SYSTEM, Dialogue, resolve_reply_quote
from wxbot_framework.conversation.context_tools import SELF_QUERY_HINTS, build_default_tools, prefetch_mention_context
from wxbot_framework.conversation.mention_tools import (IMAGE_WINDOW_SECONDS,
                                                        SEARCH_RETRY_SECONDS, MentionTools, ToolOutcome,
                                                        previous_question, public_search_topic, read_tool_options,
                                                        strip_links)
from wxbot_framework.conversation.repetition import too_similar
from wxbot_framework.conversation.turns import (COLLECT_SECONDS, TurnFocus, exchange_intent, judge_turn,
                                                likely_incomplete, recent_exchange, turn_key)
from wxbot_framework.conversation.tool_router import REJECTED, JevToolRouter
from wxbot_framework.recent_images import RECENT_IMAGES
from wxbot_framework.integrations.llm import LLMError
from wxbot_framework.conversation.members import BEIJING, SnapshotExpired
from wxbot_framework.messaging.mentions import addressed_elsewhere, at_source, bot_names, strip_at_labels, typed_at
from wxbot_framework.conversation.personality import personality_text
from wxbot_framework.conversation.utterance import (CHAT, DETAIL, QA, InterjectionGate, SpeakResult, classify_tier,
                                                    interjection_probe, speak)
from wxbot_framework.commands import command_text, is_control

GAME_FEATURE = "idiom_chain"
USED_SAMPLE = 10
# 与本群机器人最近几条发言比对重复的条数；太像时换角度重说一次。
RECENT_BOT_ROWS = 5
# 提问含这些词时视为明确要求详细回答，使用 detail_max_tokens 预算。
DETAIL_HINTS = ("详细", "详解", "细说", "展开", "具体说说", "完整", "全文", "全篇", "原文", "一步一步", "逐步")
CATCH_UP = re.compile(r"补个?课|(?:最后|最终|后来).{0,4}(?:定|说好)|(?:刚才|刚刚|昨晚|昨天).{0,8}(?:聊|讨论)")
# 框架按已编译桥接声明的图片能力；没有时找不到图要如实说“收不到图片”，而不是让对方去引用。
IMAGE_CAPABILITY = "group.image.receive"
TOOL_FOLLOWUP_LIMIT = 64


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
        if "voice.send" in getattr(ctx, "capabilities", ()):
            self.voice_reply.start(ctx.logger)

    def has_pending_callbacks(self):
        return self._slow is not None and self._slow.busy()

    def on_unload(self, ctx):
        self.voice_reply.close()
        if self._slow is not None:
            self._slow.close()

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

    def _recent_bot_texts(self, room):
        """本群机器人最近几条发言，供重复检测使用；读不到就当没有。"""
        try:
            rows = self.members.recent_rows(room, limit=RECENT_BOT_ROWS, include_bot=True,
                                            sender_id=self.members.account)
        except Exception:
            return ()
        if not isinstance(rows, (list, tuple)):
            return ()
        return tuple(row.get("text") or "" for row in rows
                     if isinstance(row, dict) and (row.get("bot_generated") or row.get("is_self")))

    def _tools(self, ctx):
        try:
            jev = ctx.jev
        except Exception as error:
            ctx.logger.warning("Jev 不可用，只按明确说法看图或搜索：%s", type(error).__name__)
            jev = None
        return MentionTools(router=JevToolRouter(jev, **self.tools), magpie=lambda: ctx.magpie,
                            images=RECENT_IMAGES, image_supported=IMAGE_CAPABILITY in getattr(ctx, "capabilities", ()),
                            logger=ctx.logger, stopped=self._slow.stopped if self._slow is not None else None)

    def _run_tools(self, message, ctx, question, snapshot=None, *, resolved_quote=None, follow=False):
        """本轮艾特的看图 / 联网；没开启时返回空结果。Jev 读不出来时只按明确说法执行。"""
        if self.tools is None:
            return ToolOutcome()
        tools = self._tools(ctx)
        # 只艾特没写正文时，他上一条发言（如“这张图是啥意思？”）才是真正的问题。
        context_question = ""
        followup_topic = None
        queued = max(0.0, time.monotonic() - message.received_at)
        now = datetime.now(timezone.utc) - timedelta(seconds=queued)
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
        elif snapshot is not None and not raw_quote and question:
            anchor, reference = recent_exchange(snapshot.history, now)
            intent = exchange_intent(question, reference) if anchor else ""
            if intent and self._turn_judge(ctx, intent, anchor, previous=reference, new=question):
                focus = replace(focus, anchor=anchor, reference=reference, clarifying=intent == "clarify")
        outcome = yield from tools.steps(question=question, room=message.chat_id, sender=message.sender_id,
                                         received_at=arrived, quote=message.data.get("quote"),
                                         image_choices=choices, previous_search=last_search, followup_topic=followup_topic,
                                         previous_subject=last_subject, focus=focus,
                                         search_date=now.astimezone(BEIJING).date().isoformat())
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
        text = strip_at_labels(message.text)
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

    def _advance_turn(self, turns, turn, ctx, *, deadline=None):
        changed = False
        for incoming in turns.additions(turn, deadline=deadline):
            source = at_source(incoming, self.members, self.aliases)
            quote = incoming.data.get("quote")
            if quote or (source is None and (incoming.data.get("at_members") or incoming.text.startswith("@"))):
                continue
            text = self._clean_query(incoming, source)
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
                message.data.get("bot_generated")):
            return
        if is_control(message.text):
            parts = command_text(message.text).split(maxsplit=1)
            args = parts[1] if len(parts) == 2 else ""
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
        if self._slow is None:
            for _ in resume():
                pass
        elif not self._slow.queue_if_active(key, resume):
            turn = resume()
            if not self._slow.handoff(key, turn):
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
        if turns is not None and turns.merged(message):
            return
        source = at_source(message, self.members, self.aliases)
        clean_query = self._clean_query(message, source)
        depth = 0
        if source is None:
            if turns is None or not clean_query or turns.active(message) or addressed_elsewhere(message):
                return
            previous = turns.continuation(message)
            if previous is None or not self._turn_judge(ctx, "follow", previous.question,
                                                       previous=previous.text, new=clean_query):
                return
            source, depth = "follow", previous.depth + 1
        elif turns is not None and not clean_query and not message.data.get("quote") and turns.active(message):
            turns.merge(message)
            return
        game = self.room_game(message.chat_id, ctx, message=message)
        if game and self.is_move(clean_query, game):
            return  # 答案由接龙插件判定。
        if turns is not None:
            turns.claim(message)
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
        turn = turns.open(message, clean_query, depth)
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
                if not turn.sent and self._advance_turn(turns, turn, ctx):
                    raise TurnChanged

            while True:
                if source == "follow" or turn.message is not message:
                    ctx.logger.info("群聊接话：来源=%s 目标=%s", source, turn.message.dispatch_sequence)
                try:
                    yield from self._reply_once(turn.message, ctx, turn.question, game, before_send=before_send,
                                                needs_review=lambda: turns.has_addition(turn),
                                                sent=lambda text: turns.sent(turn, text), plain=source == "follow")
                except TurnChanged:
                    if turn.replanned:
                        ctx.logger.info("群聊接话：按最新补充另起一轮 目标=%s", turn.message.dispatch_sequence)
                    turn.replanned = not turn.replanned
                    continue
                return
        finally:
            turns.close(turn)

    def _reply_once(self, message, ctx, clean_query, game, *, before_send=None, needs_review=None, sent=None, plain=False):
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
                    if needs_review is not None and needs_review():
                        raise TurnNeedsReview
                    yield
            quote, missing_quote = resolve_reply_quote(
                message.data.get("quote"), lambda message_id: self.members.context_text(message.chat_id, message_id))
            display = self.members.at_display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            budget = 1023 - len(("@" + display + "\u2005\n").encode("utf-8"))

            def send_part(part, send_guard=guard, *, use_at=False):
                if use_at and not plain:
                    delivered = dispatch(lambda: ctx.reply_at(message, part, display_name=display, delivery_guard=send_guard))
                else:
                    delivered = dispatch(lambda: ctx.reply(message, part, delivery_guard=send_guard))
                if delivered and sent is not None:
                    sent(part)
                return delivered

            def send_at(part, send_guard=guard):
                return send_part(part, send_guard, use_at=True)

            voice_supported = "voice.send" in getattr(ctx, "capabilities", ())
            fixed_voice = voice_supported and self.voice_reply.fixed(message, self.room_state)

            def respond(reply, tier, *, preference=False):
                use_voice = fixed_voice or (voice_supported and self.voice_reply.reserve(
                    message, reply, preference, automatic=tier == CHAT))
                if use_voice:
                    def record_voice(part):
                        result.sent.append(part)
                        if sent is not None:
                            sent(part)
                    remaining = yield from self.voice_reply.deliver(
                        message, ctx, reply, voice=personality.voice, guard=guard, split=fixed_voice,
                        dispatch=dispatch, on_sent=record_voice, fixed=fixed_voice)
                    if remaining is None:
                        return
                    reply = remaining
                gate = InterjectionGate(interjection_probe(self.members, message.chat_id)) if tier == CHAT else None
                send_guard = gate.wrap(guard) if gate is not None else guard
                speak(reply, tier=tier, byte_budget=budget, result=result,
                      send_at=lambda part: send_at(part, send_guard),
                      send_plain=lambda part: send_part(part, send_guard), interjected=gate)
                if result.failed:
                    raise RuntimeError("原生艾特发送未成功")
                if result.interrupted:
                    ctx.logger.info("闲聊拆分发送中有人插话，丢弃剩余 %d 条：room=%s", result.dropped, message.chat_id)

            if catch_up and not missing_quote:
                rows = snapshot.group_messages
                reply = Dialogue(ctx.llm_for(message, personality=personality, for_reply=True),
                                 display_timezone=ctx.display_timezone).catch_up(rows, clean_query, quote=quote)
                self._ensure_running()
                yield from respond(reply, QA)
                ctx.logger.info("群聊补课完成：记录=%d", len(rows))
                return

            detail = wants_detail(clean_query)
            tools = build_default_tools(ctx.knowledge, self.members)
            context = prefetch_mention_context(tools, message.chat_id, message.sender_id, clean_query)
            options = {"detail": True} if detail else {"examples": context.examples}
            if missing_quote:
                outcome = ToolOutcome(ask=missing_quote)
            else:
                outcome = yield from self._run_tools(message, ctx, clean_query, snapshot, resolved_quote=quote, follow=plain)
            self._ensure_running()
            if before_send is not None:
                before_send()
            if outcome.used:
                options["tool_results"] = outcome.payload()
            if outcome.context or outcome.ask:
                options["turn_context"] = "\n".join(filter(None, (outcome.context, outcome.ask)))
            can_voice = fixed_voice or (not detail and not outcome.used and voice_supported)
            if can_voice:
                options["system"] = SYSTEM + VOICE_PROMPT
                if fixed_voice:
                    options["system"] += FIXED_VOICE_PROMPT

            def generate(avoid=None, *, guidance=""):
                self._ensure_running()
                prompt_options = options if not guidance else {
                    **options, "system": options.get("system", SYSTEM) + "\n" + guidance,
                }
                text = personality_text(Dialogue(ctx.llm_for(message, personality=personality, for_reply=True)).reply(
                    snapshot, knowledge=context.knowledge, style=context.style,
                    speaker_profile=context.speaker_profile, people=context.people,
                    room_game=self.game_context(game) if game else None, quote=quote, avoid=avoid,
                    question=clean_query, **prompt_options),
                    personality.name)
                text, preference = voice_reply_format(text) if can_voice else (text, None)
                if not text:
                    raise LLMError("大模型对话结果格式不正确，请重试")
                return text, preference

            reply, voice_preference = generate()
            self._ensure_running()
            repeated = voice_preference is not True and too_similar(reply, self._recent_bot_texts(message.chat_id))
            if repeated:
                ctx.logger.info("艾特回复与最近发言相近，换角度重说一次：room=%s", message.chat_id)
                try:
                    retried, retried_preference = generate(avoid=repeated)
                except LLMError as error:
                    # 重说失败不能把已经拿到的首次结果丢掉，更不能改发错误提示。
                    ctx.logger.warning("艾特回复重说失败，按首次结果发送：%s", error)
                else:
                    reply, voice_preference = retried, retried_preference
                    if too_similar(reply, self._recent_bot_texts(message.chat_id)):
                        ctx.logger.info("艾特回复重说后仍相近，按原样发送：room=%s", message.chat_id)
            search = next((item for item in outcome.results if item.tool == "search" and item.ok), None)
            if search is not None:
                try:
                    verifier = JevToolRouter(ctx.jev)
                except ValueError:
                    verifier = JevToolRouter(None)
                evidence = strip_links(search.text)
                vision = "\n".join(item.text for item in outcome.results if item.tool == "vision" and item.ok)

                def rejected(text):
                    decision = verifier.verify_answer(question=clean_query, context=outcome.context,
                                                      evidence=evidence, answer=text, vision=vision)
                    ctx.logger.info("艾特回复证据核对：status=%s p=%s", decision.status, decision.probability)
                    return decision.status == REJECTED
                if rejected(reply):
                    reply, _ = generate(guidance="刚才的回答包含资料未支持的说法。只保留能回答本轮问题的已知内容，"
                                                 "删掉无关的额外断言；直接说具体内容，不汇报核对流程。")
                    if rejected(reply):
                        query = Dialogue(ctx.llm_for(message, personality=personality)).search_followup(
                            clean_query, context=outcome.context, evidence=evidence, answer=reply)
                        self._ensure_running()
                        followup = self._tools(ctx).search(query)
                        outcome.results.append(followup)
                        options["tool_results"] = outcome.payload()
                        evidence = "\n".join(strip_links(item.text) for item in outcome.results
                                             if item.tool == "search" and item.ok)
                        with self._followup_lock:
                            _remember_tool_followup(self._last_searches,
                                (message.session_id, message.chat_id, message.sender_id), message.received_at,
                                (query, outcome.search_subject or clean_query))
                        ctx.logger.info("艾特回复补充搜索：%s room=%s", "成功" if followup.ok else "失败", message.chat_id)
                        reply, _ = generate(guidance="结合新增搜索资料重新回答当前问题，纠正先前没有依据的说法。"
                                                     "有可靠的部分就直接答，缺口要具体到对象或比较口径，不用流程式结语。")
                        if rejected(reply):
                            reply, _ = generate(guidance="现有资料仍不足以支持刚才的具体断言，请完全放弃那些断言。"
                                "围绕当前问题，用群友的语气说清具体卡在哪里，必要时只问一个能继续查的关键条件。"
                                "不要添加新的数字、排名或其他事实结论，不说未核实、暂不下结论这类审核话术。")
            # 用了工具的回复按问答单条发出；带网址的消息容易触发微信风控，模型照抄了网址也在这里去掉。
            if outcome.used:
                reply = strip_links(reply) or reply
            tier = (DETAIL if detail else QA) if outcome.used or outcome.ask else classify_tier(clean_query, detail=detail)
            yield from respond(reply, tier, preference=voice_preference if can_voice else False)
            ctx.logger.info("艾特对话完成：room=%s persona=%s context=%d", message.chat_id,
                            personality.name, len(snapshot.messages))
        except SnapshotExpired as error:
            ctx.logger.info("艾特对话已取消：%s", error)
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
        finally:
            # 已发出至少一条（含守卫或异常发生在首条之后）就保留去重记录，避免排队中的同一提问重发。
            self._release(key, delivered=bool(result.sent))
