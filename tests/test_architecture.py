"""架构边界：依赖方向、插件隔离、服务需求声明与惰性建库。

只做静态检查与最小构造，不启动机器人、不连接微信、不请求模型。
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from support.recyclable_tempdir import RecyclableTemporaryDirectory

from wxbot_framework.app import checks
from wxbot_framework.config import ConfigStore
from wxbot_framework.conversation.members import MemberStore
from wxbot_framework.memory.knowledge import KnowledgeStore
from wxbot_framework.plugin_engine.manager import PluginManager
from wxbot_framework.sdk import PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
FRAMEWORK = ROOT / "wxbot_framework"
PLUGINS = ROOT / "plugins"

# 底层包与根级纯模块都不得反向依赖 app。
LOWER_PACKAGES = ("conversation", "memory", "storage", "messaging", "plugin_engine", "integrations")
PLAIN_MODULES = ("config", "feature_config", "commands", "authorization", "timeutil", "locking",
                 "account", "contacts", "lore", "sdk", "__init__")
# 这些插件名只应出现在插件目录、清单与配置里。
CONCRETE_PLUGINS = ("idiom_chain", "chat_summary", "mention_chat", "chat_records", "member_identity")


def module_package(path, root=ROOT):
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def imported_targets(path):
    """列出文件导入的模块名，相对导入按所在包解析为绝对名。"""
    package = module_package(path)
    targets = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                base = package[:len(package) - (node.level - 1)]
                targets.append(".".join(base + ([module] if module else [])))
            else:
                targets.append(module)
    return targets


class DependencyTests(unittest.TestCase):
    def test_lower_layers_do_not_import_app(self):
        offenders = []
        paths = [path for name in LOWER_PACKAGES for path in (FRAMEWORK / name).rglob("*.py")]
        paths += [FRAMEWORK / f"{name}.py" for name in PLAIN_MODULES]
        for path in paths:
            for target in imported_targets(path):
                if target.startswith("wxbot_framework.app"):
                    offenders.append(f"{path.relative_to(ROOT)} -> {target}")
        self.assertEqual(offenders, [])

    def test_plugin_engine_does_not_import_or_name_concrete_plugins(self):
        offenders = []
        for path in (FRAMEWORK / "plugin_engine").rglob("*.py"):
            for target in imported_targets(path):
                if target.startswith("plugins"):
                    offenders.append(f"{path.relative_to(ROOT)} -> {target}")
            text = path.read_text(encoding="utf-8-sig")
            for name in CONCRETE_PLUGINS:
                if name in text:
                    offenders.append(f"{path.relative_to(ROOT)} 提到插件名 {name}")
        self.assertEqual(offenders, [])

    def test_entry_and_app_do_not_name_concrete_plugins(self):
        offenders = []
        paths = [ROOT / "main.py"] + list((FRAMEWORK / "app").rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8-sig")
            for name in CONCRETE_PLUGINS:
                if name in text:
                    offenders.append(f"{path.relative_to(ROOT)} 提到插件名 {name}")
        self.assertEqual(offenders, [])

    def test_plugins_depend_only_on_sdk_and_shared_contracts(self):
        offenders = []
        for path in PLUGINS.rglob("*.py"):
            owner = path.relative_to(PLUGINS).parts[0]
            for target in imported_targets(path):
                if target.startswith("wxbot_framework.app"):
                    offenders.append(f"{path.relative_to(ROOT)} -> {target}")
                if target.startswith("plugins."):
                    sibling = target.split(".")[1]
                    # 只允许自己的子模块与共享契约，不得访问其它插件。
                    if sibling not in (owner, "_shared"):
                        offenders.append(f"{path.relative_to(ROOT)} -> {target}")
        self.assertEqual(offenders, [])

    def test_sdk_facade_reexports_public_names(self):
        from wxbot_framework import sdk

        for name in ("Message", "PluginContext", "CapabilityError", "ServiceFacade", "LegacyService"):
            self.assertTrue(hasattr(sdk, name), name)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = RecyclableTemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def manager_with(self, name, manifest):
        directory = self.root / "plugins" / name
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text(
            "class Plugin:\n    def on_message(self, message, ctx):\n        pass\n", encoding="utf-8")
        (directory / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        store_path = self.root / "bot_config.json"
        store_path.write_text(json.dumps({"plugins": {}}, ensure_ascii=False), encoding="utf-8")
        return PluginManager(self.root / "plugins", ConfigStore(store_path), ServiceFacade(), frozenset())

    def test_declared_services_drive_checks_without_name_checks(self):
        manager = self.manager_with("synth", {"api": "v1", "requires": [], "services": ["llm"]})
        specs = manager.discover()
        self.assertEqual(checks.required_services(specs, {"plugins": {}}), {"llm"})
        self.assertEqual(checks.required_services(specs, {"plugins": {"synth": {"enabled": False}}}), set())

    def test_manifest_without_service_declaration_still_loads(self):
        manager = self.manager_with("legacy_synth", {"api": "legacy", "requires": []})
        spec = manager.discover()["legacy_synth"]
        self.assertEqual(spec.services, frozenset())
        self.assertEqual(checks.required_services({"legacy_synth": spec}, {"plugins": {}}), set())


class LazyDatabaseTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = RecyclableTemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_reading_context_services_does_not_create_database(self):
        path = self.root / "members.json"
        path.write_text(json.dumps({"database_file": "data.sqlite3", "history_enabled": True,
                                    "owner_wxid": None, "groups": {}}, ensure_ascii=False), encoding="utf-8")
        members = MemberStore(path, "wxid_robot")
        knowledge = KnowledgeStore(members)
        context = PluginContext("probe", {}, ServiceFacade(), frozenset(),
                                member_store=members, knowledge_store=knowledge)
        self.assertIs(context.members, members)
        self.assertIs(context.knowledge, knowledge)
        self.assertIs(context.room_state, None)
        self.assertFalse(members.path.exists())


if __name__ == "__main__":
    unittest.main()
