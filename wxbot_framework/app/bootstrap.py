"""服务装配与显式初始化：在线启动、离线脚本共用同一顺序。

顺序即契约：建库与自愈 → 本地资料修订 → 知识种子 → 群状态 → 启动公告。
只有显式调用这里才会写库；解析配置或读取上下文属性不会触发建库。
停止顺序同样固定：先断原生连接，再等后台与插件，最后关闭数据库。
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..conversation.members import MemberStore
from ..feature_config import maintenance_options
from ..memory.knowledge import KnowledgeStore
from .announcements import AnnouncementQueue
from .upgrades import apply_upgrade

logger = logging.getLogger("Bot")

DEFAULT_SEED = ".runtime/group_knowledge_seed.json"


def open_knowledge(config, directory, *, display_timezone=None, seed=True):
    """离线装配成员库与知识库；调用方负责在 finally 中关闭成员库。"""
    directory = Path(directory)
    members = MemberStore.from_config(config, directory)
    if members is None:
        raise ValueError("尚未配置成员目录")
    members.initialize()
    knowledge = KnowledgeStore(
        members, display_timezone=display_timezone or config.get("display_timezone", "Asia/Shanghai"))
    knowledge.initialize()
    upgrade = maintenance_options(config)["upgrade_file"]
    if upgrade:
        apply_upgrade(knowledge, directory / upgrade)
    if seed:
        path = directory / ((config.get("memory") or {}).get("seed_file") or DEFAULT_SEED)
        if path.is_file():
            knowledge.import_seed(path)
    return members, knowledge


def startup_services(app):
    """运行前显式初始化在线服务；公告失败不影响微信连接。"""
    if app.members is None:
        return
    app.members.initialize()
    app.knowledge.initialize()
    upgrade = app.maintenance_options["upgrade_file"]
    if upgrade:
        apply_upgrade(app.knowledge, app.store.path.parent / upgrade)
    seed = app.store.path.parent / app.memory_options["seed_file"]
    if seed.is_file():
        app.knowledge.import_seed(seed)
    if app.room_state is not None:
        app.room_state.initialize()
    notices = app.maintenance_options["announcements"]
    if not notices:
        return
    for notice in notices:
        app.knowledge._room(notice["room"])
        if app.config.get("chat_allowlist") and notice["room"] not in app.config["chat_allowlist"]:
            raise ValueError("启动公告群不在当前收发范围内")
    app.startup_notice = AnnouncementQueue(app.knowledge, app.service, notices, contributors=app.contributors)
    for notice, state in zip(notices, app.startup_notice.all_status()):
        logger.info("启动公告就绪：%s -> %s（当前状态：%s）",
                    notice["id"], notice["room"], state["status"])


def release(app):
    """停止后台与插件并释放资源；未完成的部分明确报错。

    仍有后台任务存活或原生清理失败时，数据库保持打开，供残留使用者继续使用；
    清理完成、线程退出后再次调用可完成收尾。
    """
    app.stop_event.set()
    app.service.cancel_pending()
    # 先撤销微信 Hook，后台线程等待或退出失败也不会跳过原生清理。
    logger.info("正在退出：先停止微信接收并清理桥接，再等待后台任务结束")
    cleaned = app._disconnect()
    failures = []
    for name, attribute in (("维护", "maintenance_router"), ("主动群聊", "hourly_scheduler"), ("记忆", "memory_worker")):
        worker = getattr(app, attribute)
        if worker:
            try:
                worker.close()
            except Exception as error:
                failures.append(name)
                logger.error("%s线程退出未完成：%s", name, type(error).__name__)
    try:
        pending = app.plugins.shutdown()
    except Exception as error:
        failures.append("插件")
        logger.error("插件退出未完成：%s", type(error).__name__)
    else:
        if pending:
            failures.append("插件：" + "、".join(pending))
            logger.error("插件退出未完成：%s", "、".join(pending))
    # 尚存后台线程可能仍用数据库，不能强行关闭其连接。
    if app.members is not None and not failures and cleaned:
        app.members.close()
    if not cleaned:
        raise RuntimeError("原生连接未能清理；待机器人退出后运行 python tools/recover_bridge.py")
    if failures:
        raise RuntimeError("桥接已清理，但这些后台任务尚未退出：" + "、".join(failures))
    logger.info("框架已停止；连接清理：%s", app.last_cleanup)
