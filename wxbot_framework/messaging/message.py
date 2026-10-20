"""消息模型；插件不需要接触 Hook 或 DLL。"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


class CapabilityError(NotImplementedError):
    """当前后端或配置没有提供插件请求的能力。"""


@dataclass(frozen=True)
class Message:
    text: str
    chat_id: str
    sender_id: str = ""
    receiver_id: str = ""
    protocol: int = 11046
    type: int = 1
    is_group: bool = False
    is_self: bool = False
    sequence: int = 0
    session_id: str = ""
    received_at: float = field(default_factory=time.monotonic)
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # 进程内接收序号；框架入口分配，原生 sequence 仍保留用于桥接诊断。
    dispatch_sequence: int = 0
    dispatch_epoch: str = ""

    @classmethod
    def from_native(cls, protocol, data, session_id):
        snapshot = copy.deepcopy(data)
        room = snapshot.get("room_wxid") or ""
        return cls(text=snapshot.get("msg") or snapshot.get("content") or "",
                   chat_id=snapshot.get("chat_id") or room or snapshot.get("from_wxid") or "",
                   sender_id=snapshot.get("sender_id") or snapshot.get("native_sender") or snapshot.get("from_wxid") or "",
                   receiver_id=snapshot.get("native_receiver") or "", protocol=protocol,
                   is_group=bool(room), is_self=bool(snapshot.get("is_self", False)), sequence=snapshot.get("sequence", 0),
                   session_id=session_id, data=MappingProxyType(snapshot))
