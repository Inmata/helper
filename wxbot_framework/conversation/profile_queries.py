"""档案查询的短期确认；确认仅选择查询对象，不修改人物身份。"""
import re
import threading
import time
from dataclasses import replace
from types import MappingProxyType

from ..commands import command_text, is_control
from ..messaging.mentions import addressed_elsewhere

YES = frozenset({"对", "对的", "对对", "对对对", "是", "是的", "嗯", "嗯嗯", "没错", "就是他", "就是她", "就是这个人", "就是", "是他", "是她"})
NO = frozenset({"不对", "不是", "不是他", "不是她", "取消", "算了", "不用了"})


class ProfileQueries:
    def __init__(self, members, *, clock=time.monotonic):
        self.members, self.clock = members, clock
        self._lock = threading.Lock()
        self._pending = {}

    def offer(self, message, choices, snapshot, personality):
        with self._lock:
            now = self.clock()
            self._pending = {key: value for key, value in self._pending.items() if value[0] > now}
            self._pending[(message.session_id, message.chat_id, message.sender_id)] = (
                now + 300, choices, snapshot, personality)

    def clear(self):
        with self._lock:
            self._pending.clear()

    def prepare(self, message):
        if not message.is_group or message.type != 1 or message.is_self or message.data.get("bot_generated"):
            return message
        key = (message.session_id, message.chat_id, message.sender_id)
        with self._lock:
            pending = self._pending.pop(key, None)
        if pending is None:
            return message
        expires, choices, snapshot, personality = pending
        if (self.clock() > expires or is_control(message.text) or addressed_elsewhere(message) or
                not self.members.snapshot_is_current(snapshot) or
                not self.members.personality_is_current(personality)):
            return message
        answer = re.sub(r"[\s，。！？,.!?]", "", command_text(message.text)).casefold()
        chosen = None
        if answer in YES and len(choices) == 1:
            chosen = choices[0]
        elif answer.isdigit() and 1 <= int(answer) <= len(choices):
            chosen = choices[int(answer) - 1]
        else:
            for choice in choices:
                if answer in {name.casefold() for name in [choice["name"], *choice["aliases"]]}:
                    chosen = choice
                    break
        if chosen is None and answer not in NO:
            return message
        confirmation = {"entity_id": chosen["id"] if chosen else None}
        return replace(message, data=MappingProxyType({**message.data, "profile_confirmation": confirmation}))
