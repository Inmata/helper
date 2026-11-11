"""查看本群人格，管理员切换；状态在入口更新，插件负责回执。"""
from wxbot_framework.conversation.members import SnapshotExpired
from wxbot_framework.conversation.personality import switch_ack


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members

    def on_message(self, message, ctx):
        if not message.is_group or message.chat_id not in self.members.groups or message.type != 1:
            return
        if message.data.get("personality_error"):
            ctx.reply(message, message.data["personality_error"])
            return
        switched = message.data.get("personality_switch")
        state = switched or message.data.get("personality_view")
        if state is None:
            return
        try:
            text = (switch_ack(state.name) if switched else
                    f"当前人格：{state.name}\n可选：损友（熟人损友）、俏皮（俏皮损友）\n管理员发 /人格 损友 或 /人格 俏皮 切换。")
            ctx.reply(message, text, delivery_guard=lambda: ctx.personality_guard(state))
            if switched:
                ctx.logger.info("人格已切换：room=%s name=%s", state.room, state.name)
        except SnapshotExpired:
            pass
