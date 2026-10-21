"""真正艾特机器人后，用本群人格回复当前发言者。"""
from .._shared import idiom_state

from wxbot_framework.conversation.dialogue import Dialogue
from wxbot_framework.integrations.llm import LLMError
from wxbot_framework.conversation.members import SnapshotExpired
from wxbot_framework.messaging.mentions import strip_at_labels
from wxbot_framework.conversation.personality import personality_text
from wxbot_framework.conversation.summary import split_text
from wxbot_framework.commands import is_control

SELF_QUERY_HINTS = ("我是谁", "知道我是谁", "认识我吗", "我叫什么", "我的身份", "我是哪位")
GAME_FEATURE = "idiom_chain"
USED_SAMPLE = 10


class Plugin:
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
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            entity = ctx.knowledge.member_entity(message.chat_id, message.sender_id)
            profile = ctx.knowledge.speaker_profile(message.chat_id, message.sender_id)
            own = ctx.knowledge.search(message.chat_id, "", entity_id=entity["id"], limit=5, char_limit=1800)
            if len(clean_query) <= 12 and any(hint in clean_query for hint in SELF_QUERY_HINTS):
                relevant = []
            else:
                relevant = ctx.knowledge.search(message.chat_id, clean_query, limit=4, char_limit=2000)
            people = ctx.knowledge.lookup_people(message.chat_id, clean_query)
            knowledge = list({item["id"]: item for item in own + relevant}.values())
            style = ctx.knowledge.style(message.chat_id, message.sender_id)
            reply = personality_text(Dialogue(ctx.llm_for(message, personality=personality)).reply(snapshot,
                                     knowledge=knowledge, style=style, speaker_profile=profile,
                                     people=people,
                                     room_game=self.game_context(game) if game else None),
                                     personality.name)
            display = self.members.at_display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            budget = 1023 - len(("@" + display + "\u2005\n").encode("utf-8"))
            for part in split_text(reply, byte_limit=budget):
                if not ctx.reply_at(message, part, display_name=display, delivery_guard=guard):
                    raise RuntimeError("原生艾特发送未成功")
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
