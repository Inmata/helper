"""消息接收入口：把一条收到的消息交给成员库、维护通道与插件。

顺序即契约，逐段阅读即可看清一条消息的去向：

1. 唯一维护者私聊是管理通道，不要求加入群收发名单；
2. 其余消息先按 ``chat_allowlist`` 过滤；
3. ``capture`` 先编号再落库，落库慢时发题水位仍能排除已收到的答案；
4. 落库成功后通知学习线程；
5. 快速上下文清空、群维护分流；
6. 固定人格旧命令兼容，最后交给插件分发。
"""
from __future__ import annotations

import logging

from ..conversation.controls import prepare_reset
from ..conversation.personality import prepare_switch

logger = logging.getLogger("Bot")


class MessageIngress:
    """接收处理；运行时对象由应用装配，这里只按顺序调用公开接口。"""

    def __init__(self, app):
        self.app = app

    def submit(self, message):
        app = self.app
        allowed = app.config.get("chat_allowlist", [])
        # 手工装配的替身应用可能没有维护通道与学习线程。
        router = getattr(app, "maintenance_router", None)
        # 唯一维护者私聊是明确的管理通道，不要求把其个人会话加入群收发名单。
        if (router and not message.is_group and app.members.is_admin(message.sender_id) and
                message.chat_id == message.sender_id):
            router.submit(message)
            return
        if not allowed or message.chat_id in allowed:
            # 必须先编号：落库慢时，发题水位仍能排除已经收到但尚未入队的答案。
            message = app.plugins.capture(message)
            if app.members is not None:
                try:
                    inserted = app.members.record(message)
                    worker = getattr(app, "memory_worker", None)
                    if inserted and worker:
                        worker.notify()
                except Exception:
                    logger.exception("群文本保存失败，消息继续分发到插件")
            if message.data.get("dispatch_to_plugins", True):
                message = prepare_reset(message, app.members)
                if router and router.submit(message):
                    return
                if app.plugins.is_active("personality"):
                    message = prepare_switch(message, app.members)
                app.plugins.dispatch(message)
