"""长期运行、连接监督与本地控制台命令。"""
from __future__ import annotations

import logging
import queue
import signal
import threading
import time

from ..integrations.wechat import CAPABILITIES, TEXT_CAPABILITIES, NativeFileHelperBackend, NativeTextBackend
from ..plugin_engine.manager import PluginManager
from ..sdk import ServiceFacade
from ..conversation.members import MemberStore
from ..memory.knowledge import KnowledgeStore
from ..conversation.room_state import RoomStateStore
from ..feature_config import feature_options
from ..feature_config import maintenance_options
from .announcements import send_startup_messages
from .bootstrap import release, startup_services
from .control import RuntimeControl
from .console import ConsoleCommands
from .ingress import MessageIngress
from .maintenance import MaintenanceRouter, MaintenanceService

logger = logging.getLogger("Bot")


class BotApplication:
    def __init__(self, root, store, *, backend_factory=None):
        self.root, self.store = root, store
        self.config = store.load()
        self.display_timezone = self.config.get("display_timezone", "Asia/Shanghai")
        self.service = ServiceFacade(text_delay=self.config.get("text_send_delay"))
        self.members = MemberStore.from_config(self.config, self.store.path.parent)
        self.knowledge = (KnowledgeStore(self.members, display_timezone=self.display_timezone)
                          if self.members is not None else None)
        self.room_state = RoomStateStore(self.members) if self.members is not None else None
        self.memory_options, self.hourly_options = feature_options(self.config)
        self.memory_worker = self.hourly_scheduler = None
        self.maintenance_options = maintenance_options(self.config)
        self.maintenance = (MaintenanceService(self.knowledge, control=RuntimeControl(self),
                                                options=self.maintenance_options)
                            if self.knowledge else None)
        self.maintenance_router = MaintenanceRouter(self.maintenance, self.service) if self.maintenance else None
        self.startup_notice = None
        self.startup_greeting = None
        self._startup_messages_attempted = False
        self._background_lock = threading.RLock()
        generic = self.config.get("backend") == "native_text"
        capabilities = TEXT_CAPABILITIES if generic else CAPABILITIES
        self.plugins = PluginManager(root / "plugins", store, self.service, capabilities,
                                     self.config.get("plugin_queue_size", 128), member_store=self.members,
                                     knowledge_store=self.knowledge, room_state_store=self.room_state)
        # 插件发现后复用与发送相同的署名快照校验公告长度。
        self.contributors = self.plugins.contributions()
        self.maintenance_options = maintenance_options(self.config, contributors=self.contributors)
        if self.maintenance is not None:
            self.maintenance.options = self.maintenance_options
        backend_type = NativeTextBackend if generic else NativeFileHelperBackend
        self.backend_factory = backend_factory or (lambda: backend_type(root, self.config))
        self.backend = None
        self.stop_event = threading.Event()
        self.commands = queue.Queue()
        self.last_cleanup = None
        self._connection_ready = False
        self._last_connection_log = 0
        self._last_connection_error = ""
        # 控制台命令只做解析；命令实现见 app/console.py。
        self.console = ConsoleCommands(self)

    def _log_connection_error(self, error):
        text = f"{type(error).__name__}: {error}"
        now = time.monotonic()
        if text != self._last_connection_error or now - self._last_connection_log >= 30:
            logger.warning("等待微信连接：%s", text)
            self._last_connection_error, self._last_connection_log = text, now

    def _connect(self):
        try:
            candidate = self.backend_factory()
            self.backend = candidate
            self.service.bind(candidate)
            candidate.start(self._emit)
            self._connection_ready = True
            self._last_connection_error = ""
            logger.info("微信已连接，PID=%s；收发范围：%s", candidate.pid,
                        getattr(candidate, "scope", "文本"))
            self.sync_wechat_directory()
        except Exception as error:
            self._connection_ready = False
            self._log_connection_error(error)
            self._disconnect()
        if self._connection_ready:
            # 公告失败不影响微信连接；本次运行只继续尚未成功的公告。
            self._send_startup_messages()

    def sync_wechat_directory(self, room=None):
        if not self._connection_ready or self.backend is None or self.members is None:
            return {"members": 0, "names": 0}
        reader = getattr(self.backend, "read_room_directory", None)
        if not callable(reader):
            return {"members": 0, "names": 0}
        rooms = [room] if room else sorted(self.members.groups)
        total_members, total_names = 0, 0
        for target_room in rooms:
            try:
                members, names = reader(target_room)
                if members or names:
                    self.members.sync_room_directory(target_room, members, names)
                    total_members += len(members)
                    total_names += len(names)
            except Exception:
                logger.warning("同步群成员缓存暂缓：room=%s", target_room)
        return {"members": total_members, "names": total_names}

    def _emit(self, message):
        """后端回调入口；处理顺序见 ``app/ingress.py``。"""
        return MessageIngress(self).submit(message)

    def _disconnect(self):
        self._connection_ready = False
        try:
            self.last_cleanup = self.service.close()
            self.backend = None
            return True
        except Exception as error:
            # 清理忙时保留实例，只重试清理，不覆盖 DLL、不再次注入。
            self._log_connection_error(error)
            return False

    def _send_startup_messages(self):
        """首次连上后处理启动公告；没有待发公告时补一句问候。"""
        if self._startup_messages_attempted:
            return
        self._startup_messages_attempted = True
        send_startup_messages(self.startup_notice, self.startup_greeting)

    def execute(self, command):
        """控制台命令入口；解析与执行见 ``app/console.py``。"""
        return self.console.execute(command)

    def _read_console(self):
        """后台读取标准输入；实现见 ``app/console.py``。"""
        return self.console.read_stdin()

    def _start_background(self):
        if self.stop_event.is_set():
            return
        if not (self.memory_options["enabled"] or self.hourly_options["enabled"]):
            return
        if self.members is None or not self.members.history_enabled or self.members.daily_clear:
            raise ValueError("长期记忆和定时聊天需要成员记录，history_enabled=true、daily_clear=false")
        from ..integrations.llm import LLMClient
        client = LLMClient.from_file(self.store.path.parent / self.config.get("llm_config_file", "llm_config.json"))
        if self.memory_options["enabled"] and not self.memory_worker:
            from ..memory.worker import MemoryWorker
            self.memory_worker = MemoryWorker(self.knowledge, client, batch_size=self.memory_options["batch_size"],
                                              flush_seconds=self.memory_options["flush_seconds"],
                                              display_timezone=self.display_timezone, jev=self._jev_client(),
                                              persona_refresh_hours=self.memory_options["persona_refresh_hours"],
                                              persona_days=self.memory_options["persona_days"])
            self.memory_worker.start()
        if self.hourly_options["enabled"] and not self.hourly_scheduler:
            from ..memory.hourly import HourlyScheduler
            options = {key: value for key, value in self.hourly_options.items() if key != "enabled"}
            self.hourly_scheduler = HourlyScheduler(
                self.knowledge, self.service, client, **options,
                allowlist=self.config.get("chat_allowlist", []),
                stale_minutes=lambda: self.hourly_options["stale_candidate_minutes"],
                ready=lambda: self._connection_ready)
            self.hourly_scheduler.start()

    def _jev_client(self):
        """人格补充的安全筛查客户端；配置缺失时关闭归纳，只记错误类型，不回显密钥。"""
        if not self.memory_options.get("persona_refresh_hours"):
            return None
        from ..integrations.jev import JevClient
        try:
            return JevClient.from_file(self.store.path.parent / self.config.get("llm_config_file", "llm_config.json"))
        except Exception as error:
            logger.warning("人格补充已关闭：Jev 配置不可用 error=%s", type(error).__name__)
            return None

    def update_feature(self, section, changes):
        with self._background_lock:
            if self.stop_event.is_set():
                raise ValueError("机器人正在退出，未修改后台设置")
            candidate = dict(self.config)
            candidate[section] = {**candidate.get(section, {}), **changes}
            self.store.validate(candidate)
            memory, hourly = feature_options(candidate)
            display_timezone = candidate.get("display_timezone", "Asia/Shanghai")
            if memory["enabled"] or hourly["enabled"]:
                if not self.members or not self.members.history_enabled or self.members.daily_clear:
                    raise ValueError("后台功能需要持久化的群聊记录")
                from ..integrations.llm import LLMClient
                LLMClient.from_file(self.store.path.parent / candidate.get("llm_config_file", "llm_config.json"))
            if section == "memory" and self.memory_worker:
                self.memory_worker.close()
                self.memory_worker = None
            if section == "hourly_chat" and self.hourly_scheduler:
                self.hourly_scheduler.close()
                self.hourly_scheduler = None
            self.config = self.store.update_section(section, changes)
            if self.knowledge:
                self.display_timezone = display_timezone
                self.knowledge.display_timezone = self.display_timezone
                self.maintenance.display_timezone = self.display_timezone
            self.memory_options, self.hourly_options = feature_options(self.config)
            self._start_background()

    def run(self, *, seconds=0):
        started = time.monotonic()
        next_connect = 0
        next_rollover = 0
        try:
            startup_services(self)
            self._start_background()
            self.plugins.reload()
            if self.maintenance_router:
                self.maintenance_router.start()
            if self.config.get("console", True):
                threading.Thread(target=self._read_console, name="BotConsole", daemon=True).start()
            logger.info("框架已启动；输入 help 查看插件管理命令，Ctrl+C 退出")
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self.members is not None and now >= next_rollover:
                    self.members.rollover()
                    next_rollover = now + 0.5
                if seconds and now - started >= seconds:
                    break
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.execute(command)
                    except Exception:
                        logger.exception("控制台命令执行失败")
                if self.stop_event.is_set():
                    break
                if self._connection_ready and self.backend.last_error:
                    self._log_connection_error(self.backend.last_error)
                    self._disconnect()
                    next_connect = now + self.config.get("reconnect_seconds", 5)
                elif not self._connection_ready and now >= next_connect:
                    if self.backend is None or self._disconnect():
                        self._connect()
                    next_connect = time.monotonic() + self.config.get("reconnect_seconds", 5)
                self.stop_event.wait(0.1)
        except KeyboardInterrupt:
            logger.info("收到退出指令")
        finally:
            self.stop()

    def stop(self):
        previous_interrupt = None
        if threading.current_thread() is threading.main_thread():
            previous_interrupt = signal.signal(signal.SIGINT, lambda *_: logger.warning("正在清理退出，请等待，不要关闭窗口"))
        try:
            release(self)
        finally:
            if previous_interrupt is not None:
                signal.signal(signal.SIGINT, previous_interrupt)
