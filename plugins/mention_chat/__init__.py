"""真正艾特机器人后，用本群人格回复当前发言者。"""
import threading
import time

from .._shared import idiom_state

from wxbot_framework.conversation.dialogue import Dialogue
from wxbot_framework.conversation.context_tools import SELF_QUERY_HINTS, build_default_tools, prefetch_mention_context
from wxbot_framework.integrations.llm import LLMError
from wxbot_framework.conversation.members import SnapshotExpired
from wxbot_framework.messaging.mentions import strip_at_labels
from wxbot_framework.conversation.personality import personality_text
from wxbot_framework.conversation.utterance import (CHAT, InterjectionGate, SpeakResult, classify_tier,
                                                    interjection_probe, speak)
from wxbot_framework.commands import is_control

GAME_FEATURE = "idiom_chain"
USED_SAMPLE = 10
# 提问含这些词时视为明确要求详细回答，使用 detail_max_tokens 预算。
DETAIL_HINTS = ("详细", "详解", "细说", "展开", "具体说说", "完整", "全文", "全篇", "原文", "一步一步", "逐步")


def wants_detail(query):
    return any(hint in query for hint in DETAIL_HINTS)


class Plugin:
    def __init__(self):
        # 同一群、同一发言者、相同提问的回复在生成或发送期间不重复生成。
        # key -> (首次提问到达时刻, 生成开始时刻, 生成结束时刻)；生成中时结束时刻为 None。
        self._pending = {}
        self._pending_lock = threading.Lock()

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
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。

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

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                "WXBOT_AT_CHAT_PROBE" in message.text or
                message.data.get("bot_generated") or is_control(message.text) or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ())):
            return
        personality = ctx.personality(message)
        guard = lambda: ctx.personality_guard(personality)
        clean_query = strip_at_labels(message.text).strip()
        game = self.room_game(message.chat_id, ctx, message=message)
        if game and self.is_move(clean_query, game):
            return  # 答案由接龙插件判定并出题，人格不重复回复也不编造进度。
        key = (message.chat_id, message.sender_id, clean_query)
        if not self._claim(key, message.received_at):
            ctx.logger.info("艾特对话跳过：相同提问仍在回复中 room=%s", message.chat_id)
            return
        result = SpeakResult()
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            detail = wants_detail(clean_query)
            tools = build_default_tools(ctx.knowledge, self.members)
            context = prefetch_mention_context(tools, message.chat_id, message.sender_id, clean_query)
            options = {"detail": True} if detail else {"examples": context.examples}
            reply = personality_text(Dialogue(ctx.llm_for(message, personality=personality)).reply(snapshot,
                                     knowledge=context.knowledge, style=context.style,
                                     speaker_profile=context.speaker_profile, people=context.people,
                                     room_game=self.game_context(game) if game else None, **options),
                                     personality.name)
            display = self.members.at_display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            budget = 1023 - len(("@" + display + "\u2005\n").encode("utf-8"))
            tier = classify_tier(clean_query, detail=detail)
            # 闲聊拆分时把插话检测并入守卫：后续条在拿到守卫后、原生发送前再查一次。
            gate = InterjectionGate(interjection_probe(self.members, message.chat_id)) if tier == CHAT else None
            send_guard = gate.wrap(guard) if gate is not None else guard
            speak(reply, tier=tier, byte_budget=budget, result=result,
                  send_at=lambda part: ctx.reply_at(message, part, display_name=display, delivery_guard=send_guard),
                  send_plain=lambda part: ctx.reply(message, part, delivery_guard=send_guard),
                  interjected=gate)
            if result.failed:
                raise RuntimeError("原生艾特发送未成功")
            if result.interrupted:
                ctx.logger.info("闲聊拆分发送中有人插话，丢弃剩余 %d 条：room=%s", result.dropped, message.chat_id)
            ctx.logger.info("艾特对话完成：room=%s persona=%s context=%d", message.chat_id,
                            personality.name, len(snapshot.messages))
        except SnapshotExpired:
            ctx.logger.info("艾特对话已取消：记录或人格状态已变更")
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
            try:
                ctx.reply(message, f"暂时无法回复：{error}", delivery_guard=guard)
            except SnapshotExpired:
                pass
        finally:
            # 已发出至少一条（含守卫或异常发生在首条之后）就保留去重记录，避免排队中的同一提问重发。
            self._release(key, delivered=bool(result.sent))
