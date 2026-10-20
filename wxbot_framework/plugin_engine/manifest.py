"""插件清单解析与贡献元数据；不依赖运行时、具体插件或应用层。

清单新增可选的 ``services`` / ``optional_services`` 声明，与后端 ``requires``
能力分开：旧清单缺少声明时按“需求未知”处理，仍可正常加载。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import validate_name

CONTRIBUTOR_FALLBACK = "Inmata"


@dataclass(frozen=True)
class PluginSpec:
    name: str
    path: Path
    api: str
    description: str
    requires: frozenset
    author: str = ""
    pull_request_author: str = ""
    contributors: tuple = ()
    services: frozenset = frozenset()
    optional_services: frozenset = frozenset()


def _string_list(manifest, key, *, required=False):
    if key not in manifest:
        return None
    value = manifest[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"plugin.json 的 {key} 应为字符串数组")
    if required and not value:
        raise ValueError(f"plugin.json 的 {key} 不能为空数组")
    return frozenset(value)


def read_manifest(directory):
    """读取插件目录的 ``plugin.json``；文件缺失时按默认值处理。"""
    path = Path(directory) / "plugin.json"
    manifest = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(manifest, dict):
        raise ValueError("plugin.json 应为 JSON 对象")
    api = manifest.get("api", "v1")
    requires = manifest.get("requires", ["text.receive"])
    if api not in ("v1", "legacy") or not isinstance(requires, list) or not all(isinstance(x, str) for x in requires):
        raise ValueError("plugin.json 的 api / requires 不合法")
    author = manifest.get("author", "")
    pr_author = manifest.get("pull_request_author", "")
    contributors = manifest.get("contributors", [])
    if (not isinstance(author, str) or not isinstance(pr_author, str) or
            not isinstance(contributors, list) or
            not all(isinstance(item, str) for item in contributors)):
        raise ValueError("plugin.json 的作者字段应为字符串或字符串列表")
    return {
        "api": api,
        "requires": requires,
        "description": str(manifest.get("description", "")),
        "author": author.strip(),
        "pull_request_author": pr_author.strip(),
        "contributors": tuple(item.strip() for item in contributors),
        "services": _string_list(manifest, "services") or frozenset(),
        "optional_services": _string_list(manifest, "optional_services") or frozenset(),
    }


def spec_for(directory, root):
    """把插件目录解析为 ``PluginSpec``；目录名即插件名。"""
    directory, root = Path(directory), Path(root)
    name = validate_name(directory.name)
    path = directory / "__init__.py"
    if not path.is_file():
        raise ValueError("插件目录缺少 __init__.py")
    if not path.resolve().is_relative_to(root):
        raise ValueError("插件源文件必须位于插件目录内")
    manifest = read_manifest(directory)
    return PluginSpec(name, path, manifest["api"], manifest["description"],
                      frozenset(manifest["requires"]), manifest["author"],
                      manifest["pull_request_author"], manifest["contributors"],
                      manifest["services"], manifest["optional_services"])


def contribution_rows(contributors=None):
    """读取同一份运行时插件清单；缺项再从仓库清单兼容回退。"""
    if contributors is not None:
        rows = contributors.values() if isinstance(contributors, dict) else contributors
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("插件贡献配置不正确")
            name = row.get("plugin")
            if not isinstance(name, str) or not name:
                raise ValueError("插件贡献配置缺少插件名")
            result[name] = row
        return result

    root = Path(__file__).resolve().parent.parent.parent
    result = {}
    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        for directory in sorted(plugins_dir.iterdir()):
            manifest_path = directory / "plugin.json"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            result[directory.name] = {
                "plugin": directory.name,
                "author": manifest.get("author", ""),
                "pull_request_author": manifest.get("pull_request_author", ""),
                "contributors": manifest.get("contributors", []),
            }
    fallback_path = root / "plugin_contributors.json"
    if fallback_path.is_file():
        data = json.loads(fallback_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("plugin_contributors.json 应为对象")
        for name, row in data.items():
            if not isinstance(row, dict):
                raise ValueError(f"插件贡献配置不正确：{name}")
            result.setdefault(name, {"plugin": name, **row})
    return result


def announcement_contributions(plugins, contributors=None):
    """按公告声明顺序生成署名，任一字段缺失都回退默认维护者。"""
    data = contribution_rows(contributors)
    lines = []
    for name in plugins:
        row = data.get(name, {"plugin": name})
        author = row.get("author") or CONTRIBUTOR_FALLBACK
        pr_author = row.get("pull_request_author") or CONTRIBUTOR_FALLBACK
        actual = row.get("contributors") or [CONTRIBUTOR_FALLBACK]
        if isinstance(actual, str):
            actual = [actual]
        if (not isinstance(author, str) or not author.strip() or
                not isinstance(pr_author, str) or not pr_author.strip() or
                not isinstance(actual, (list, tuple)) or
                any(not isinstance(item, str) or not item.strip() for item in actual)):
            raise ValueError(f"插件贡献配置不正确：{name}")
        lines.append(
            f"{name}：作者 {author.strip()}；PR 提交者 {pr_author.strip()}；"
            f"实际贡献者 {'、'.join(item.strip() for item in actual)}")
    return "\n".join(lines)


def announcement_text(notice, contributors=None):
    """人工正文在前，插件贡献由配置声明的顺序确定性追加。"""
    text = (notice.get("text") or "").strip()
    return text + ("\n" if text else "") + announcement_contributions(
        notice.get("plugins", ()), contributors)
