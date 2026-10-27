"""本地控制台命令：解析并调用应用的公开能力。

命令集合与语义保持不变；这里只做解析与调用，不持有运行状态。
"""
from __future__ import annotations

import json
import logging
import shlex
import sys

from ..authorization import LOCAL_EDITOR

logger = logging.getLogger("Bot")
PERSONA_ACTIONS = ("show", "history", "rollback", "base", "derive")


class ConsoleCommands:
    """控制台命令执行器；运行时对象由应用装配。"""

    def __init__(self, app):
        self.app = app

    def execute(self, command):
        app = self.app
        words = shlex.split(command)
        if not words:
            return
        action = words[0].lower()
        if action == "manage":
            if not app.maintenance:
                raise ValueError("尚未配置成员目录与知识库")
            arguments = words[1:]
            if arguments[:1] == ["--room"]:
                arguments[0] = "--群"
            result = app.maintenance.execute(shlex.join(arguments), LOCAL_EDITOR)
            logger.info("维护结果：%s", result)
        elif action in ("quit", "exit", "stop") and len(words) == 1:
            app.stop_event.set()
        elif action == "help" and len(words) == 1:
            logger.info("命令：manage [--room 群ID] 帮助 / status / chats / members [群ID] / history 称呼 [条数] / persona [history|rollback|base|derive] [群ID] / plugins / enable 名称 / disable 名称 / reload [名称] / quit")
        elif action == "status" and len(words) == 1:
            logger.info("状态：%s", json.dumps({"connected": app._connection_ready,
                                               "pid": app.backend.pid if app.backend else None,
                                               "native": app.backend.diagnostics() if app.backend and hasattr(app.backend, "diagnostics") else {},
                                               "plugins": app.plugins.status(),
                                               "knowledge": app.knowledge.status() if app.knowledge else None}, ensure_ascii=False))
        elif action == "chats" and len(words) == 1:
            rows = app.backend.chats() if app.backend and hasattr(app.backend, "chats") else []
            logger.info("本次连接已接收的会话：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "members" and len(words) in (1, 2):
            if app.members is None:
                raise ValueError("尚未配置成员目录")
            rows = [{"name": m.name, "display_name": m.display_name, "wxid": m.wxid,
                     "room": m.room, "bound": bool(m.wxid)} for m in app.members.list_members(words[1] if len(words) == 2 else None)]
            logger.info("成员目录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "history" and len(words) in (2, 3):
            if app.members is None:
                raise ValueError("尚未配置成员目录")
            rows = app.members.history(words[1], limit=int(words[2]) if len(words) == 3 else 10)
            logger.info("成员聊天记录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "persona" and len(words) <= 3:
            self.persona(words[1:])
        elif action == "plugins" and len(words) == 1:
            for row in app.plugins.status():
                state = "运行" if row["active"] else "停用"
                logger.info("%s [%s] 已处理=%s 异常=%s 丢弃=%s %s", row["name"], state,
                            row["handled"], row["errors"], row["dropped"], row["error"])
        elif action in ("enable", "disable") and len(words) == 2:
            getattr(app.plugins, action)(words[1])
        elif action == "reload" and len(words) in (1, 2):
            app.plugins.reload(words[1] if len(words) == 2 else None)
        else:
            raise ValueError("未知命令；输入 help 查看用法")

    def persona(self, arguments):
        """人格补充：查看、历史、回退上一版、回到基准、立即归纳；回退不删除任何版本。"""
        app = self.app
        if app.members is None:
            raise ValueError("尚未配置成员目录")
        sub = arguments[0] if arguments and arguments[0] in PERSONA_ACTIONS else "show"
        rest = arguments[1:] if sub != "show" or (arguments and arguments[0] == "show") else arguments
        if len(rest) > 1:
            raise ValueError("用法：persona [history|rollback|base|derive] [群ID]")
        room = self._persona_room(rest[0] if rest else None)
        members = app.members
        if sub == "show":
            state = members.personality(room)
            logger.info("人格补充：%s", json.dumps({"room": room, "version": state.generation,
                                                   "supplement": state.supplement or None}, ensure_ascii=False))
        elif sub == "history":
            rows = [{"version": row["version"], "created_at": row["created_at"], "source": row["source"],
                     "active": row["active"], "chars": len(row["text"])} for row in members.persona_versions(room)]
            logger.info("人格补充历史（当前版本=%s）：%s", members.personality(room).generation,
                        json.dumps(rows, ensure_ascii=False))
        elif sub == "rollback":
            logger.info("人格补充已回退：当前版本=%s（0 为基准）", members.rollback_persona(room))
        elif sub == "base":
            members.activate_persona(room, 0)
            logger.info("人格补充已回到基准人格；历史版本保留")
        else:
            deriver = getattr(app.memory_worker, "persona", None)
            if deriver is None:
                raise ValueError("人格补充归纳未启用（需开启 memory、persona_refresh_hours>0 且 Jev 配置可用）")
            result = deriver.derive(room, source="console")
            reasons = {}
            for _, _, reason in result.dropped:
                reasons[reason] = reasons.get(reason, 0) + 1
            logger.info("人格补充归纳：%s", json.dumps({"status": result.status, "version": result.version,
                                                     "messages": result.message_count, "kept": result.kept,
                                                     "dropped": reasons}, ensure_ascii=False))

    def _persona_room(self, room):
        groups = sorted(self.app.members.groups)
        if room is None:
            if len(groups) != 1:
                raise ValueError("配置了多个群，请写明群ID")
            return groups[0]
        if room not in groups:
            raise ValueError("该群不在配置中")
        return room

    def read_stdin(self):
        app = self.app
        # stdin 关闭时仍继续运行，适合后台启动。
        try:
            for line in sys.stdin:
                if app.stop_event.is_set():
                    break
                app.commands.put(line.strip())
        except (OSError, ValueError):
            pass
