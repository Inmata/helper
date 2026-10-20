"""启动前检查：配置、清单、插件语法与服务需求。

只读取本地配置与插件目录；不建库、不注入微信、不请求模型。
服务需求由插件清单声明，入口不再按插件名判断。
"""
from __future__ import annotations

import json

from ..integrations.llm import LLMClient
from ..memory.knowledge import read_seed
from .upgrades import read_upgrade


def enabled_specs(specs, config):
    """按发现顺序产出 (spec, 是否启用)；清单未声明的插件默认启用。"""
    for spec in specs.values():
        yield spec, config.get("plugins", {}).get(spec.name, {}).get("enabled", True)


def required_services(specs, config):
    """启用插件声明的服务需求集合。"""
    services = set()
    for spec, enabled in enabled_specs(specs, config):
        if enabled:
            services |= set(spec.services)
    return services


def _llm_config(store, config):
    return store.path.parent / config.get("llm_config_file", "llm_config.json")


def check_runtime(app, store, config):
    """长期记忆、知识种子、资料修订与维护目标的可用性检查。"""
    if app.memory_options["enabled"] or app.hourly_options["enabled"]:
        if app.members is None or not app.members.history_enabled or app.members.daily_clear:
            raise ValueError("长期记忆需要 history_enabled=true、daily_clear=false")
        LLMClient.from_file(_llm_config(store, config))
    seed = store.path.parent / app.memory_options["seed_file"]
    if seed.is_file():
        read_seed(seed)
    upgrade = app.maintenance_options["upgrade_file"]
    if upgrade:
        if app.knowledge is None:
            raise ValueError("资料修订需要成员目录和知识库")
        read_upgrade(app.knowledge, store.path.parent / upgrade)
    if app.maintenance_options["default_room"]:
        if app.maintenance is None:
            raise ValueError("维护目标需要成员目录")
        app.maintenance.room()
    if app.maintenance_options["announcements"]:
        if app.maintenance is None:
            raise ValueError("更新公告需要成员目录")
        for notice in app.maintenance_options["announcements"]:
            app.maintenance.room(explicit=notice["room"])


def check_plugins(app, store, config, specs):
    """编译启用插件源码，并按清单声明的服务需求解析配置。"""
    if "llm" in required_services(specs, config):
        LLMClient.from_file(_llm_config(store, config))
    for spec, enabled in enabled_specs(specs, config):
        if enabled:
            for source in spec.path.parent.rglob("*.py"):
                compile(source.read_bytes(), str(source), "exec")


def run(app, store, config, *, check):
    """执行 --check / --list-plugins，打印结果并返回进程退出码。"""
    if check:
        check_runtime(app, store, config)
    specs = app.plugins.discover()
    if app.plugins.load_errors:
        raise ValueError(str(app.plugins.load_errors))
    if check:
        check_plugins(app, store, config, specs)
    for spec, enabled in enabled_specs(specs, config):
        print(json.dumps({"name": spec.name, "enabled": enabled, "api": spec.api,
                          "description": spec.description,
                          "missing_capabilities": sorted(spec.requires - app.plugins.capabilities)},
                         ensure_ascii=False))
    if check:
        print("配置与插件语法检查通过")
    return 0
