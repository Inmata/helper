"""停止流程的退出确认：插件退出未完成时明确报告，且不主动关闭数据库。"""
from __future__ import annotations

import json
import logging
import threading
import time
import unittest
from pathlib import Path

from support.recyclable_tempdir import RecyclableTemporaryDirectory

from wxbot_framework.app.bootstrap import release
from wxbot_framework.app.runtime import BotApplication
from wxbot_framework.config import ConfigStore

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"

BLOCKING_PLUGIN = '''"""受控阻塞的假插件：卸载时记录一次并等待测试放行。"""
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _append(name):
    path = HERE / name
    path.write_text((path.read_text(encoding="utf-8") if path.exists() else "") + "1", encoding="utf-8")


class Plugin:
    def on_message(self, message, ctx):
        pass

    def on_unload(self, ctx):
        _append("unloads.txt")
        for _ in range(120):
            if (HERE / "release").exists():
                return
            time.sleep(0.05)
'''

MEMBERS = {"database_file": "data.sqlite3", "history_enabled": True, "owner_wxid": None, "groups": {}}


def blocker_thread_alive():
    return any(thread.name == "Plugin.blocker" and thread.is_alive() for thread in threading.enumerate())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = RecyclableTemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        blocker = self.root / "plugins/blocker"
        blocker.mkdir(parents=True)
        (blocker / "__init__.py").write_text(BLOCKING_PLUGIN, encoding="utf-8")
        (blocker / "plugin.json").write_text(json.dumps({"name": "blocker", "api": "v1", "requires": []}),
                                             encoding="utf-8")
        self.blocker = blocker
        (self.root / "members.json").write_text(json.dumps(MEMBERS, ensure_ascii=False), encoding="utf-8")
        config = json.loads((ROOT / "bot_config.example.json").read_text(encoding="utf-8"))
        config.update(console=False, members_config_file="members.json", wechat_account="wxid_robot")
        config["memory"] = {"enabled": False}
        config["hourly_chat"] = {"enabled": False}
        config["plugins"] = {"blocker": {"enabled": True}}
        self.path = self.root / "bot_config.json"
        self.path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.app = BotApplication(self.root, ConfigStore(self.path))

    def tearDown(self):
        logging.disable(logging.NOTSET)
        if blocker_thread_alive():
            (self.blocker / "release").write_text("", encoding="utf-8")
            deadline = time.monotonic() + 5
            while blocker_thread_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
        self.app.members.close()
        self.temporary.cleanup()

    def test_blocking_plugin_is_reported_and_database_stays_open_until_it_exits(self):
        app = self.app
        app.members.initialize()
        app.plugins.reload()
        self.assertTrue(blocker_thread_alive())
        app._disconnect = lambda: True
        with self.assertRaisesRegex(RuntimeError, "blocker"):
            release(app)
        self.assertFalse(app.members.database.closed)
        self.assertTrue(blocker_thread_alive())
        # 放行插件退出后，再次停止才关闭数据库，且卸载只执行一次。
        (self.blocker / "release").write_text("", encoding="utf-8")
        deadline = time.monotonic() + 5
        while blocker_thread_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(blocker_thread_alive())
        release(app)
        self.assertTrue(app.members.database.closed)
        self.assertEqual((self.blocker / "unloads.txt").read_text(encoding="utf-8"), "1")

    def test_second_stop_still_reports_pending_plugin_and_keeps_database_open(self):
        app = self.app
        app.members.initialize()
        app.plugins.reload()
        self.assertTrue(blocker_thread_alive())
        app._disconnect = lambda: True
        with self.assertRaisesRegex(RuntimeError, "blocker"):
            release(app)
        self.assertFalse(app.members.database.closed)
        # 插件线程仍存活时再次停止，仍须报告未完成：未退出的句柄不能被丢弃后误判为已收尾。
        with self.assertRaisesRegex(RuntimeError, "blocker"):
            release(app)
        self.assertFalse(app.members.database.closed)
        self.assertTrue(blocker_thread_alive())
        (self.blocker / "release").write_text("", encoding="utf-8")
        deadline = time.monotonic() + 5
        while blocker_thread_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(blocker_thread_alive())
        release(app)
        self.assertTrue(app.members.database.closed)
        self.assertEqual((self.blocker / "unloads.txt").read_text(encoding="utf-8"), "1")

    def test_native_cleanup_failure_keeps_database_open(self):
        app = self.app
        app.members.initialize()
        app.plugins.reload()
        app._disconnect = lambda: False
        with self.assertRaisesRegex(RuntimeError, "recover_bridge.py"):
            release(app)
        self.assertFalse(app.members.database.closed)
        self.assertTrue(blocker_thread_alive())


if __name__ == "__main__":
    unittest.main()
