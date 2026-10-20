"""维护命令需要的运行控制能力。

只暴露公开动作与只读选项，不把应用内部状态交给维护代码；离线维护没有
运行控制时，需要在线能力的命令明确报错。
"""
from __future__ import annotations


class RuntimeControl:
    """在线运行控制：由应用装配，维护服务只依赖这里的接口。"""

    def __init__(self, app):
        self._app = app

    @property
    def display_timezone(self):
        return self._app.config.get("display_timezone", "Asia/Shanghai")

    @property
    def allowlist(self):
        return self._app.config.get("chat_allowlist", [])

    @property
    def connected(self):
        return self._app._connection_ready

    @property
    def memory_options(self):
        return self._app.memory_options

    @property
    def hourly_options(self):
        return self._app.hourly_options

    @property
    def announcements(self):
        """本次运行的启动公告队列；未配置时为 None。"""
        return self._app.startup_notice

    def sync_directory(self, room):
        return self._app.sync_wechat_directory(room)

    def update_feature(self, section, changes):
        return self._app.update_feature(section, changes)

    def request_learning_now(self):
        """请求立即处理积压消息；未开启自动学习时返回 False。"""
        worker = self._app.memory_worker
        if worker is None:
            return False
        worker.request_immediate()
        return True

    def plugin_status(self):
        return self._app.plugins.status()

    def set_plugin_enabled(self, name, enabled):
        getattr(self._app.plugins, "enable" if enabled else "disable")(name)

    def reload_plugins(self, name=None):
        return self._app.plugins.reload(name)
