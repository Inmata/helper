"""知识查询与维护；权限始终由实际消息作者判断。"""
from contextlib import contextmanager

from wxbot_framework.commands import command_text, help_text
from wxbot_framework.conversation.members import MemberError, SnapshotExpired, parse_aliases
from wxbot_framework.conversation.profiles import profile_messages, suggest_people, write_profile
from wxbot_framework.conversation.slow_replies import SlowReplies
from wxbot_framework.conversation.summary import split_text
from wxbot_framework.integrations.llm import LLMError
from wxbot_framework.timeutil import format_local


class Plugin:
    def on_load(self, ctx):
        self.members, self.knowledge = ctx.members, ctx.knowledge
        ctx.llm
        self._slow = SlowReplies(ctx.logger, name="Knowledge")

    def has_pending_callbacks(self):
        return self._slow.busy()

    def on_unload(self, ctx):
        self._slow.close()
        self.members.profile_queries.clear()

    def on_message(self, message, ctx):
        if (not message.is_group or message.type != 1 or message.chat_id not in self.members.groups or
                message.data.get("bot_generated")):
            return
        parts = command_text(message.text).split(maxsplit=1)
        if message.data.get("profile_confirmation") or (parts and parts[0] == "/档案"):
            argument = parts[1].strip() if len(parts) == 2 else ""
            resume = lambda: self._profile_turn(message, ctx, argument)
            key = (message.session_id, message.chat_id, message.sender_id)
            if not self._slow.queue_if_active(key, resume):
                turn = resume()
                if not self._slow.handoff(key, turn):
                    turn.close()
            return
        if not parts or parts[0] not in ("/记忆", "/纠正", "/忘记", "/关联人物", "/help"):
            return
        command, argument = parts[0], parts[1].strip() if len(parts) == 2 else ""
        try:
            text = self.handle(command, argument, message)
        except (MemberError, ValueError) as error:
            text = str(error)
        for part in split_text(text):
            ctx.reply(message, part)

    def _profile_turn(self, message, ctx, argument):
        anchor = self.members.message_id(message) or self.members.latest_id(message.chat_id) + 1
        snapshot = self.members.recent_snapshot(message.chat_id, limit=50, before_id=anchor, context=True)
        personality = ctx.personality(message)

        @contextmanager
        def guard():
            with ctx.response_guard(snapshot, personality):
                if self._slow.stopped.is_set():
                    raise SnapshotExpired("档案插件已停止")
                yield

        try:
            with guard():
                pass
            blocks = None
            confirmation = message.data.get("profile_confirmation")
            if confirmation:
                if confirmation["entity_id"] is None:
                    ctx.reply(message, "好，先不查。可以重新发 /档案 名字，或 /档案 @群友。", delivery_guard=guard)
                    return
                entity_id = confirmation["entity_id"]
            else:
                targets = [wxid for wxid in message.data.get("at_members", ()) if wxid != self.members.account]
                if len(targets) > 1:
                    raise MemberError("一次查一位群友，请发 /档案 @群友。")
                if targets or not argument:
                    entity = self.knowledge.member_entity(message.chat_id, targets[0] if targets else message.sender_id)
                else:
                    try:
                        entity = self.knowledge.find_entity(message.chat_id, argument)
                    except MemberError:
                        recent = [{"speaker": row["speaker_name"], "text": row["text"]} for row in snapshot.messages]
                        choices = suggest_people(ctx.llm, argument,
                                                 self.knowledge.profile_candidates(message.chat_id), recent)
                        if not choices:
                            question = "这个称呼我还没认出来。能补一个名字，或者发 /档案 @群友 吗？"
                        elif len(choices) == 1:
                            question = f"你说的是{choices[0]['name']}吗？回复“对”就查，认错了回“不是”。"
                        else:
                            question = "你想查哪一位？回复序号或名字：\n" + "\n".join(
                                f"{index}. {choice['name']}" for index, choice in enumerate(choices, 1))
                        if ctx.reply(message, question, delivery_guard=guard) and choices:
                            self.members.profile_queries.offer(message, choices, snapshot, personality)
                        return
                if entity["entity_key"].startswith("group:"):
                    items = self.knowledge.search(message.chat_id, "", entity_id=entity["id"])
                    blocks = [self.format_profile(entity["name"], "本群公共记忆", entity, items, None,
                                                  self.members.is_admin(message.sender_id))]
                else:
                    entity_id = entity["id"]
            if blocks is None:
                material = self.knowledge.profile_material(message.chat_id, entity_id)
                blocks = write_profile(ctx.llm_for(message, personality=personality, for_reply=True), material)
            for part in profile_messages(blocks):
                if not ctx.reply(message, part, delivery_guard=guard):
                    raise RuntimeError("档案文本发送未成功")
                yield
            ctx.logger.info("档案查询完成：room=%s sections=%d", message.chat_id, len(blocks) - 1)
        except SnapshotExpired:
            ctx.logger.info("档案查询已取消：上下文、人格或连接失效")
        except (MemberError, LLMError) as error:
            ctx.logger.warning("档案查询失败：%s", type(error).__name__)
            try:
                ctx.reply(message, str(error) if isinstance(error, MemberError) else
                          "这次档案没整理好，请稍后再发 /档案 查询。", delivery_guard=guard)
            except SnapshotExpired:
                pass

    def handle(self, command, argument, message):
        room, sender = message.chat_id, message.sender_id
        if command == "/help":
            return help_text(self.members.is_admin(sender))
        if command == "/关联人物":
            if not self.members.is_admin(sender):
                raise MemberError("只有管理员可以关联人物")
            targets = [wxid for wxid in message.data.get("at_members", ()) if wxid != self.members.account]
            from wxbot_framework.messaging.mentions import strip_at_labels
            if targets:
                name = strip_at_labels(argument).strip()
                if len(targets) != 1 or not name:
                    raise MemberError("用法：/绑定 @群友 人物名")
                target_wxid = targets[0]
            else:
                tokens = argument.split(maxsplit=1)
                if len(tokens) != 2:
                    raise MemberError("用法：/绑定 <@群友|M序号|微信昵称|wxid> <人物名>")
                target_wxid = self.members.resolve_member_target(room, tokens[0])
                name = tokens[1].strip()
            entity = self.knowledge.link(room, target_wxid, name, sender, create_if_missing=True)
            return f"已关联：{self.members.wechat_name(target_wxid) or self.members.display_name(target_wxid, room) or '该成员'} → {entity['name']}。"
        if command in ("/纠正", "/忘记"):
            values = argument.split(maxsplit=1)
            if (not values or not values[0].isdigit() or len(values) != (1 if command == "/忘记" else 2)):
                raise MemberError("用法：/忘记 编号" if command == "/忘记" else "用法：/纠正 编号 新内容")
            self.knowledge.edit(room, int(values[0]), sender, forget=command == "/忘记",
                                content=values[1] if len(values) == 2 else None)
            return "已停用该记忆，聊天原文仍保留。" if command == "/忘记" else "已纠正并保存修订记录。"
        if command == "/记忆":
            if not argument:
                raise MemberError("用法：/记忆 关键词")
            items = self.knowledge.search(room, argument)
            return (self.format_items(items, self.knowledge.display_timezone)
                    if items else "还没有找到相关的长期记忆。")

    @staticmethod
    def format_profile(name, state, entity, items, style, is_admin=False):
        aliases = parse_aliases(entity["aliases"])
        lines = [f"{name}（{state}）"]
        if aliases:
            lines.append("别名：" + "、".join(aliases))
        if style:
            lines.append(f"表达习惯（{style['sample_count']}条样本）：{style['description']}")
        else:
            lines.append("表达习惯：样本不足（至少30条）或已被停用。")
        if items:
            lines.append("重要记忆：")
            for item in items:
                prefix = f"#{item['id']} " if is_admin else ""
                lines.append(f"· {prefix}{item['content']}（{item['kind']}）")
        else:
            lines.append("尚无可展示的长期资料。")
        return "\n".join(lines)

    @staticmethod
    def format_items(items, display_timezone=None):
        lines = []
        for item in items:
            sources = item["sources"]
            date = format_local(
                sources[0]["source_time"] if sources else item["updated_at"],
                display_timezone, pattern="%Y-%m-%d")
            if any(source["origin"].startswith("seed:") for source in sources):
                origin = "space整理"
            elif any((source["origin"] or "").startswith("manual:") for source in sources):
                origin = "人工维护"
            else:
                origin = "聊天记录"
            lines.append(f"· {item['name']}｜{item['kind']}：{item['content']}（{origin}，{date}）")
        return "\n\n".join(lines)
