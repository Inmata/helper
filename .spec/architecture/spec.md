---
title: 架构
related:
  - main.py
  - wxbot_framework/app/bootstrap.py
  - tests/test_architecture.py
  - tests/test_lifecycle.py
---

# 架构

项目边界、目录职责、依赖方向、资源归属与扩展落点；具体行为见各功能 spec：
[messages](../messages/spec.md)、[delivery](../delivery/spec.md)、[learning](../learning/spec.md)、
[lifecycle](../lifecycle/spec.md)、[announcements](../announcements/spec.md)、
[mention-reply](../mention-reply/spec.md)、[idiom-chain](../idiom-chain/spec.md)。

## 边界

- 运行平台为 Windows x64 与 64 位 Python；原生桥接负责文本收发，C++ 内部不在本规范范围。
- 统一入口是 `main.py`：解析参数、装配应用、启动主循环。
- 功能以插件实现；配置字段、用户命令、脚本入口与插件目录名保持兼容。
- spec 只写已经实现的行为；计划中的改动留在 `docs/` 的计划文档。

## 目录职责

| 目录 / 文件 | 职责 |
|---|---|
| `main.py` | 统一入口：参数、日志、检查分支、启动 |
| `wxbot_framework/sdk.py` | 插件可用接口的稳定导出 |
| `wxbot_framework/config.py`、`feature_config.py` | 配置读写与纯校验、功能选项解析 |
| `commands.py` / `authorization.py` / `timeutil.py` / `locking.py` / `account.py` / `contacts.py` / `lore.py` | 根级纯工具 |
| `app/` | 装配与显式初始化（`bootstrap.py`）、主循环（`runtime.py`）、消息顺序（`ingress.py`）、控制台、维护、检查命令、公告、资料修订 |
| `messaging/` | 消息模型、发送、节奏、接收水位、文本与艾特工具 |
| `plugin_engine/` | 清单、发现与加载、插件上下文 |
| `conversation/` | 成员与历史、群状态、对话、总结、人格、清空控制、决策接缝（`planner.py`） |
| `memory/` | 知识库与维护、增量学习、学习调度、主动发言 |
| `storage/` | 唯一连接与事务、集中迁移 |
| `integrations/` | 原生微信后端适配、大模型与 Jev 客户端 |
| `plugins/_shared/` | 插件之间的只读数据契约 |
| `plugins/_template/` | 插件模板 |
| `native/`、`tests/`、`tools/` | 原生桥接源码与 DLL、离线测试、离线维护脚本 |
| `docs/` | 计划、评测与架构配图（`docs/images/` 为准，`docs/diagrams/*.mmd` 为源） |

## 依赖方向

![分层依赖：main → app → 功能包 → storage/sdk；plugins 通过 sdk 与 _shared 接入](../../docs/images/architecture-layers.png)

| 规则 | 说明 |
|---|---|
| 装配只发生在 `app` | `main → app → 功能模块 / 插件引擎 / 适配器`；底层不得导入 `app` |
| 单向依赖 | `memory` 可用 `conversation` 的公开成员、历史与版本接口；`conversation` 不反向依赖 `memory` |
| 引擎不认识插件 | `plugin_engine` 负责发现、加载、生命周期与分发，不引用具体插件；入口不按插件名判断服务需求 |
| 插件边界 | 插件通过 `wxbot_framework.sdk` 与公开功能接口工作；插件之间只共享 `plugins/_shared/` 的只读契约 |
| 静态检查 | 以上边界由 `tests/test_architecture.py` 用 AST 静态检查 |

## 资源归属

- `storage.Database` 是唯一的 SQLite 连接：持有连接、可重入锁、可嵌套事务与备份；成员、历史、知识库与群状态共用，不各自开连接。
- 事务由用例控制：下层写方法只执行 SQL，异常沿调用栈抛出，不留半批结果。
- 只有显式初始化会建库与迁移：`app/bootstrap.py` 的 `startup_services()` 与 `open_knowledge()`；解析配置、读取插件上下文属性不写数据库。
- 停止顺序与未完成时的处理见 [lifecycle](../lifecycle/spec.md)；检查命令（`--check` / `--list-plugins`）不建库、不注入微信、不请求模型。

| 线程名 | 作用 |
|---|---|
| `BotConsole` | 读取标准输入的命令行 |
| `Maintenance` | 维护命令队列 |
| `MemoryLearner` | 增量学习调度 |
| `HourlyChat` | 定时主动发言候选（冷场兜底） |
| `ProactiveChat.timer` | 事件驱动主动发言的去抖计时（插件 `proactive_chat` 自建，`on_unload` 时停止） |
| `Plugin.<名称>` | 每个插件的消息队列 |

## 扩展落点

- 普通插件：复制 `plugins/_template/`，目录名即插件名；`plugin.json` 声明 `api`、`requires`，需要模型时声明 `"services": ["llm"]`；实现 `Plugin.on_message(message, ctx)`，可选 `on_load`、`on_unload`；跨插件数据放 `plugins/_shared/`。
- 持久状态：表结构加进 `storage/migrations.py`，保持既有表、主键与升级次序；归属某个功能包并使用注入的同一个 `Database`；补“写入失败不留半批状态”的离线测试。
- 后台任务：在 `memory/` 下实现，提供 `close()` 并能在超时后报告尚未退出；在 `app/bootstrap.py` 的 `release()` 顺序中登记，线程名加入上表与配图。

## 验收

- 全量离线测试：`python -B -m unittest discover -s tests`。
- 入口检查：`python -B main.py --config <合成配置> --check` 与 `--list-plugins`。
- 依赖边界与插件隔离：`tests/test_architecture.py`。
