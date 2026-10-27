"""没人艾特时的事件驱动主动发言：群里一停顿，先过代码闸门，再让 Jev 判断要不要插一句。

流程：on_message 记下待判断的群 → 计时线程在群里停顿 ``quiet_seconds``（或首条起满 ``max_wait_seconds``）
后取出 → ``SpeakBudget.check``（10 分钟配额、间隔、连续次数）→ ``JevSpeakPlanner.decide`` 返回
reply / wait / skip → 只有 reply 才用本群人格生成一句，经 ``utterance.speak`` 以闲聊档发出。
艾特机器人的消息交给 mention_chat，不在这里回复，也不占配额。
"""
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .._shared import idiom_state

from wxbot_framework.commands import is_control
from wxbot_framework.conversation.context_tools import pick_speech_examples
from wxbot_framework.conversation.members import BEIJING, SnapshotExpired
from wxbot_framework.conversation.personality import personality_text
from wxbot_framework.conversation.planner import (REPLY, PERSONA_SUMMARY, JevSpeakPlanner, build_speak_context,
                                                  generate_proactive)
from wxbot_framework.conversation.utterance import CHAT, InterjectionGate, SpeakResult, interjection_probe, speak
from wxbot_framework.integrations.llm import LLMError
from wxbot_framework.timeutil import parse_utc

GAME_FEATURE = "idiom_chain"
# (配置键, 默认值, 下限, 上限)；数值都是整数，阈值按百分比填写。
OPTIONS = (
    ("quiet_seconds", 3, 1, 60),
    ("max_wait_seconds", 8, 1, 120),
    ("threshold_percent", 60, 1, 100),
    ("streak_step_percent", 10, 0, 50),
    ("limit", 5, 1, 30),
    ("window_seconds", 600, 60, 86400),
    ("gap_seconds", 60, 0, 3600),
    ("max_streak", 2, 1, 10),
    ("streak_reset_messages", 3, 1, 50),
    ("context_limit", 15, 3, 50),
    ("start_hour", 8, 0, 23),
    ("end_hour", 24, 1, 24),
)


def read_options(config):
    values = {}
    for key, default, lower, upper in OPTIONS:
        value = config.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"proactive_chat.{key} 应为 {lower}–{upper} 的整数")
        values[key] = value
    if values["start_hour"] >= values["end_hour"]:
        raise ValueError("proactive_chat.start_hour 应早于 end_hour")
    if values["max_wait_seconds"] < values["quiet_seconds"]:
        raise ValueError("proactive_chat.max_wait_seconds 不应小于 quiet_seconds")
    rooms = config.get("rooms", [])
    if not isinstance(rooms, list) or not all(isinstance(room, str) and room for room in rooms):
        raise ValueError("proactive_chat.rooms 应为群 ID 列表（空表示全部启用的群）")
    values["rooms"] = frozenset(rooms)
    return values


class Plugin:
    def on_load(self, ctx):
        self._thread = None
        self._stop = threading.Event()
        self.active = ctx.config.get("enabled") is True
        if not self.active:
            # 主动发言会在群里说话：只有在配置里显式写 "enabled": true 才启动，缺省保持静默。
            ctx.logger.info("主动发言未显式启用，保持静默")
            return
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("主动发言需要 history_enabled: true")
        self.options = read_options(ctx.config)
        self.room_state = getattr(ctx, "room_state", None)
        ctx.llm  # 启动时核对配置，不请求接口。
        self.planner = JevSpeakPlanner(ctx.jev, threshold=self.options["threshold_percent"] / 100,
                                       streak_step=self.options["streak_step_percent"] / 100)
        # 预算挂在成员库上，与定时主动发言（memory/hourly.py）共用同一份计数。
        self.budget = self.members.speak_budget
        self.budget.configure(limit=self.options["limit"], window_seconds=self.options["window_seconds"],
                              gap_seconds=self.options["gap_seconds"], max_streak=self.options["max_streak"],
                              streak_reset_messages=self.options["streak_reset_messages"])
        self.clock = time.monotonic
        self._pending = {}  # room -> [首条到达, 最后一条到达, 最后一条消息]
        self._versions = {}  # room -> 群友消息版本：判断、生成与发送守卫据此作废旧一轮
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, args=(ctx,), name="ProactiveChat.timer", daemon=True)
        self._thread.start()

    def on_unload(self, ctx):
        self._stop.set()
        if self.active:
            # 发送守卫在聊天记录锁内检查停止标志：拿一次这把锁，已越过检查的那一条发完后，之后的段都会被拒。
            with self.members.lock:
                pass
        if self._thread is not None:
            # 阻塞中的 Jev/模型请求可能超过等待时间；发送守卫会检查停止标志，卸载后不再发出。
            self._thread.join(timeout=65)

    def _enabled_room(self, room):
        return room in self.members.groups and (not self.options["rooms"] or room in self.options["rooms"])

    def on_message(self, message, ctx):
        if (not self.active or message.type != 1 or not message.is_group or not self._enabled_room(message.chat_id) or
                message.data.get("bot_generated") or message.is_self or is_control(message.text)):
            return
        room = message.chat_id
        # 群友消息计入预算由 MemberStore.record 统一完成（定时主动发言共用），这里不重复计。
        with self._lock:
            # 任何群友新消息（含艾特）都让正在判断、生成或发送的一轮作废。
            self._versions[room] = self._versions.get(room, 0) + 1
            if message.data.get("is_at_self"):
                # 艾特由 mention_chat 回复；这一轮不再主动插话。
                self._pending.pop(room, None)
                return
            now = self.clock()
            entry = self._pending.get(room)
            if entry is None:
                self._pending[room] = [now, now, message]
            else:
                entry[1], entry[2] = now, message

    def _due(self):
        """取出已停顿的群：返回 [(room, 最后一条消息, 版本)]。"""
        now = self.clock()
        due = []
        with self._lock:
            for room, (first, last, message) in list(self._pending.items()):
                if now - last >= self.options["quiet_seconds"] or now - first >= self.options["max_wait_seconds"]:
                    del self._pending[room]
                    due.append((room, message, self._versions.get(room, 0)))
        return due

    def _run(self, ctx):
        while not self._stop.wait(0.5):
            for room, message, version in self._due():
                if self._stop.is_set():
                    return
                try:
                    self.consider(room, message, ctx, version=version)
                except SnapshotExpired:
                    ctx.logger.info("主动发言已取消：记录或人格状态已变更 room=%s", room)
                except LLMError as error:
                    ctx.logger.warning("主动发言生成失败：%s", error)
                except Exception as error:
                    ctx.logger.warning("主动发言暂缓：room=%s error=%s", room, type(error).__name__)

    def _game_active(self, room):
        if self.room_state is None:
            return False
        try:
            state = self.room_state.get(room, GAME_FEATURE)
        except Exception:
            return False
        if idiom_state.current_and_used(state) is None:
            return False
        age = idiom_state.updated_age(state)
        idle = state.get("idle_seconds", 1800)
        return age is not None and type(idle) is int and 0 <= age <= idle

    def _bot_names(self, room):
        account = self.members.account
        return tuple(name for name in (self.members.wechat_name(account), self.members.display_name(account, room))
                     if name)

    def _superseded(self, room, version):
        """判断、生成或发送期间群里又有群友新消息，或插件正在卸载：这一轮作废。"""
        if self._stop.is_set():
            return True
        with self._lock:
            return self._versions.get(room, 0) != version

    def _fresh_guard(self, room, version, newer, guard):
        """发送守卫：在原守卫内、原生发送前再核对一次，使判定贴近真实发送时刻。

        原守卫持有聊天记录锁，群消息落库也要拿这把锁：守卫内读到的记录水位之后的消息，
        必然在这次原生发送完成之后才落库，因而 ``newer()`` 能可靠地排除检查与发送之间的空档。
        """
        @contextmanager
        def guarded():
            with guard():
                if self._superseded(room, version) or newer():
                    raise SnapshotExpired("主动发言期间群里有新消息或插件已卸载")
                yield
                # 原生发送正常返回后仍在聊天记录锁内记账（数据库锁 → 预算锁）：此后到达的群友消息才计入新一轮；
                # 原生调用返回假值也记，是偏保守的一侧。
                self.budget.mark_sent(room)
        return guarded

    def consider(self, room, message, ctx, *, version=None):
        """判断并在需要时发出一次主动发言；返回是否发出。"""
        if version is None:
            with self._lock:
                version = self._versions.get(room, 0)
        local = datetime.now(BEIJING)
        if not self.options["start_hour"] <= local.hour < self.options["end_hour"]:
            return False
        refused = self.budget.check(room)
        if refused:
            ctx.logger.info("主动发言跳过：room=%s reason=%s", room, refused)
            return False
        if self._game_active(room):
            ctx.logger.info("主动发言跳过：room=%s reason=idiom-game", room)
            return False
        # 水位绑定触发这一轮的消息自己的序号：它之后落库的消息（含已落库但尚未分发给插件的）都算新消息。
        # 消息还没落库（None）时无从界定“之后”，这一轮放弃。
        baseline = self.members.message_id(message)
        if type(baseline) is not int:
            return False
        # 先取快照再读记录：判断和生成期间 /清空、跨天或有新消息落库时，发送守卫会拒发。
        snapshot = self.members.recent_snapshot(room, limit=1, context=True)
        newer = interjection_probe(self.members, room, baseline=baseline)
        rows = self.members.recent_rows(room, limit=self.options["context_limit"], include_bot=True, context=True)
        if not rows or rows[-1].get("bot_generated") or rows[-1].get("is_self"):
            return False
        for row in rows:
            row["received_at"] = parse_utc(row["received_at"])
        persona = self.members.personality(room)
        context = build_speak_context(rows, now=datetime.now(timezone.utc), bot_names=self._bot_names(room),
                                      persona=PERSONA_SUMMARY + (persona.supplement and "\n" + persona.supplement),
                                      streak=self.budget.streak(room), local_time=local.strftime("%H:%M"))
        if newer():
            # 判断前就已有更新的消息：不浪费 Jev 调用，交给下一轮。
            ctx.logger.info("主动发言跳过：room=%s reason=newer-message", room)
            return False
        action = self.planner.decide(context)
        args = action.args
        ctx.logger.info("主动发言判断：room=%s action=%s p=%s threshold=%s top=%s seconds=%s reason=%s",
                        room, action.type, args.get("probability"), args.get("threshold"), args.get("top"),
                        args.get("seconds"), args.get("reason", ""))
        if action.type != REPLY or self._superseded(room, version):
            return False
        model = ctx.llm_for(message, personality=persona)
        text = personality_text(generate_proactive(model, context.lines,
                                                   examples=pick_speech_examples(self.members, room)))
        if not text:
            ctx.logger.info("主动发言跳过：room=%s reason=nothing-to-say", room)
            return False
        if self._superseded(room, version):
            ctx.logger.info("主动发言跳过：room=%s reason=superseded", room)
            return False
        # 检查与占位一步完成：与定时主动发言共用预算，同群同时只有一个主动发言在发。
        refused = self.budget.reserve(room)
        if refused:
            ctx.logger.info("主动发言跳过：room=%s reason=%s", room, refused)
            return False
        result = SpeakResult()

        def send(part):
            # 首条记账在发送守卫内完成（见 _fresh_guard），这里不再记。
            return ctx.reply(message, part, delivery_guard=guard)
        try:
            gate = InterjectionGate(interjection_probe(self.members, room))
            guard = gate.wrap(self._fresh_guard(room, version, newer,
                                                lambda: ctx.response_guard(snapshot, persona)))
            speak(text, tier=CHAT, byte_budget=1023, interjected=gate, result=result, send_at=send)
        finally:
            # 拆分发送中途抛异常时，已发出的部分照样计入配额。
            self.budget.release(room, sent=bool(result.sent))
        if result.interrupted:
            ctx.logger.info("主动发言拆分发送中有人插话，丢弃剩余 %d 条：room=%s", result.dropped, room)
        if result.failed and not result.sent:
            ctx.logger.warning("主动发言发送未成功：room=%s", room)
            return False
        ctx.logger.info("主动发言完成：room=%s persona=%s count=%d", room, persona.name, self.budget.count(room))
        return bool(result.sent)
