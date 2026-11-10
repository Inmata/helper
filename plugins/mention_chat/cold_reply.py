"""对指定成员限时启用短句回击。"""
import re
from time import time

from wxbot_framework.account import validate_account


COMMAND = "/冷处理"
FEATURE = "mention_cold_reply"
INTERVAL = 600
USAGE = "用法：/冷处理 <@群友|M序号|微信ID> <30m|2h|0m>"
LINES = (
    "收到，今天的演出我就看到这儿。",
    "你这段循环播放，我这边就不续费了。",
    "这话题挺耐聊，我就不续杯了。",
    "你接着发挥，我就不抢镜了。",
)


class ColdReply:
    def __init__(self, members, room_state):
        self.members = members
        self.room_state = room_state

    def command(self, message, ctx, args):
        if not self.members.is_admin(message.sender_id):
            ctx.reply(message, "仅维护者可以使用 /冷处理。")
            return
        parts = args.rsplit(maxsplit=1)
        duration = re.fullmatch(r"([0-9]+)([hm])", parts[-1]) if len(parts) == 2 else None
        if not duration:
            ctx.reply(message, USAGE)
            return
        target = parts[0]
        at_members = message.data.get("at_members", ())
        if at_members:
            if len(at_members) != 1 or not re.fullmatch(r"@[^@\r\n\u2005]+", target):
                ctx.reply(message, USAGE)
                return
            target = at_members[0]
        elif target.startswith("@"):
            ctx.reply(message, "请从微信成员列表艾特一位群友，或填写微信 ID、M序号。")
            return
        elif re.fullmatch(r"[Mm][0-9]+", target):
            try:
                target = self.members.resolve_member_target(message.chat_id, target)
            except ValueError as error:
                ctx.reply(message, str(error))
                return
        now = time()
        try:
            validate_account(target)
            seconds = int(duration[1]) * (3600 if duration[2] == "h" else 60)
            until = now + seconds
        except (ValueError, OverflowError):
            ctx.reply(message, USAGE)
            return
        if target == self.members.account:
            ctx.reply(message, "不能对机器人自己设置冷处理。")
            return

        room = message.chat_id
        state = self.room_state.get(room, FEATURE) or {}
        state = {wxid: entry for wxid, entry in state.items() if entry["until"] > now}
        if seconds:
            state[target] = {"until": until, "next_reply": now, "line": 0}
        else:
            state.pop(target, None)
        if state:
            self.room_state.put(room, FEATURE, state)
        else:
            self.room_state.clear(room, FEATURE)
        ctx.reply(message, f"本群冷处理已设置，持续 {parts[1]}。" if seconds else "本群冷处理已解除。")

    def active(self, message):
        state = self.room_state.get(message.chat_id, FEATURE) or {}
        entry = state.get(message.sender_id)
        return bool(entry and entry["until"] > time())

    def handle_mention(self, message, ctx):
        state = self.room_state.get(message.chat_id, FEATURE) or {}
        entry = state.get(message.sender_id)
        now = time()
        if not entry or entry["until"] <= now:
            return False
        if now >= entry["next_reply"] and ctx.reply_at(message, LINES[entry["line"]]):
            entry["next_reply"] = time() + INTERVAL
            entry["line"] = (entry["line"] + 1) % len(LINES)
            self.room_state.put(message.chat_id, FEATURE, state)
        return True
