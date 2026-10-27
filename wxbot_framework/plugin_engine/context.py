"""插件上下文：只暴露已装配的服务和稳定的回复接口。"""
from __future__ import annotations

import copy
import logging
from contextlib import contextmanager, nullcontext
from types import MappingProxyType

from ..messaging.message import CapabilityError


class PluginContext:
    def __init__(self, name, options, service, capabilities, *, llm_config_path=None, member_store=None,
                 knowledge_store=None, room_state_store=None, display_timezone=None, message_watermarks=None):
        self.name = name
        self.config = MappingProxyType(copy.deepcopy(options))
        self.logger = logging.getLogger(f"Plugin.{name}")
        self.capabilities = frozenset(capabilities)
        self._service = service
        self._llm_config_path = llm_config_path
        self._llm = None
        self._jev = None
        self._members = member_store
        self._knowledge = knowledge_store
        self._room_state = room_state_store
        self.display_timezone = display_timezone
        self._message_watermarks = message_watermarks

    def message_watermark(self, room):
        """当前群已进入框架的最大接收序号，包含仍在落库或排队的消息。"""
        return self._message_watermarks.latest(room) if self._message_watermarks is not None else 0

    @property
    def message_epoch(self):
        return self._message_watermarks.epoch if self._message_watermarks is not None else ""

    @property
    def members(self):
        if self._members is None:
            raise CapabilityError("尚未配置框架成员目录")
        return self._members

    @property
    def room_state(self):
        """按群共享的插件状态；未配置成员目录时为 None，插件需自行降级。"""
        return self._room_state

    @property
    def knowledge(self):
        if self._knowledge is None:
            raise CapabilityError("尚未配置知识库；知识库由应用启动时装配")
        return self._knowledge

    @property
    def llm(self):
        if self._llm is None:
            if self._llm_config_path is None:
                raise ValueError("尚未配置大模型文件")
            from ..integrations.llm import LLMClient
            self._llm = LLMClient.from_file(self._llm_config_path)
        return self._llm

    @property
    def jev(self):
        """Jev 判断客户端：读同一份大模型配置的 "jev" 段，缺省项取主模型配置。"""
        if self._jev is None:
            if self._llm_config_path is None:
                raise ValueError("尚未配置大模型文件")
            from ..integrations.jev import JevClient
            self._jev = JevClient.from_file(self._llm_config_path)
        return self._jev

    def send_text(self, chat_id, text):
        sent = self._service.send_text(chat_id, text)
        if sent:
            self._record_bot_reply(chat_id, text)
        return sent

    def _record_bot_reply(self, chat_id, text):
        """成功发出的机器人回复也进入聊天记录，供多轮对话引用自己的上一轮。"""
        if self._members is None:
            return
        try:
            self._members.record_bot_reply(chat_id, text)
        except Exception:
            self.logger.debug("机器人回复未能落库：chat_id=%s", chat_id)

    def personality(self, message):
        from ..conversation.personality import PersonalityState
        return self._members.personality(message.chat_id) if self._members is not None else PersonalityState(message.chat_id)

    def llm_for(self, message, *, personality=None):
        from ..conversation.personality import PersonalityLLM
        state = personality if personality is not None else self.personality(message)
        return PersonalityLLM(self.llm, state, current=lambda: self._members is None or
                              self._members.personality_is_current(state))

    def personality_guard(self, state):
        return self._members.personality_delivery_guard(state) if self._members is not None else nullcontext()

    @contextmanager
    def response_guard(self, snapshot, personality):
        with self.members.delivery_guard(snapshot), self.personality_guard(personality):
            yield

    def reply(self, message, text, *, delivery_guard=None):
        sent = self._service.send_text(message.chat_id, text, expected_session=message.session_id,
                                       delivery_guard=delivery_guard)
        if sent:
            self._record_bot_reply(message.chat_id, text)
        return sent

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        if not message.is_group:
            raise ValueError("reply_at 仅用于群消息")
        if self._members is not None:
            member = self._members.find_by_wxid(message.sender_id, message.chat_id)
            if member is not None and (display_name is None or member.self_named):
                display_name = member.display_name
            elif display_name is None:
                display_name = self._members.wechat_name(message.sender_id)
        sent = self._service.send_at_text(message.chat_id, text, [message.sender_id],
                                         expected_session=message.session_id,
                                         display_names=[display_name] if display_name is not None else None,
                                         delivery_guard=delivery_guard)
        if sent:
            self._record_bot_reply(message.chat_id, text)
        return sent
