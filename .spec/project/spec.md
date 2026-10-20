---
title: 微信机器人框架
related:
  - messages/spec.md
  - learning/spec.md
  - delivery/spec.md
  - lifecycle/spec.md
---

# 项目规范

根节点说明项目边界、依赖方向与资源归属，不绑定单个实现文件；具体约定见各子规范。

## 边界

- 运行平台为 Windows x64 与 64 位 Python；原生桥接负责文本收发，C++ 内部不在本规范范围。
- 统一入口是 `main.py`：解析参数、装配应用、启动主循环。
- 功能以插件实现；配置字段、用户命令、脚本入口与插件目录名保持兼容。
- 本规范描述**已经实现并验证**的约定；尚未验证的目标另列，不写成既有保证。

## 依赖方向

| 规则 | 说明 |
|---|---|
| 装配只发生在 `app` | `main → app → 功能模块 / 插件引擎 / 适配器`；底层不得导入 `app` |
| 单向依赖 | `memory` 可以使用 `conversation` 的公开成员、历史与版本接口；`conversation` 不反向依赖 `memory` |
| 引擎不认识插件 | `plugin_engine` 负责发现、加载、生命周期与分发，不引用具体插件 |
| 插件边界 | 插件通过 `wxbot_framework.sdk` 与公开功能接口工作；插件之间只共享 `plugins/_shared/` 中的只读数据契约 |
| 静态检查 | 底层不得导入 `app`、插件边界等由 `tests/test_architecture.py` 用 AST 静态检查 |

## 资源归属

- `storage.Database` 是唯一的 SQLite 连接：持有连接、可重入锁、可嵌套事务与备份。
- 成员与聊天历史共用该连接；知识库与群状态使用同一实例，不各自开连接。
- 只有显式初始化会建库与迁移：`app/bootstrap.py` 的 `startup_services()` 与 `open_knowledge()`。
  解析配置、读取插件上下文属性不写数据库。
- 停止顺序固定：标记停止 → 取消发送 → 断开并清理原生桥接 → 等待后台任务与插件 → 关闭数据库。
  仍有使用者存活或原生清理失败时报告停止未完成，不主动关闭数据库。

## 失败行为

- 未完成的停止不静默成功：抛出说明原因的 `RuntimeError`，并保留资源句柄供再次收尾。
- 检查命令（`--check` / `--list-plugins`）不建库、不注入微信、不请求模型。

## 验收

- 全量离线测试：`python -B -m unittest discover -s tests`。
- 入口检查：`python -B main.py --config <合成配置> --check` 与 `--list-plugins`。
- 依赖边界与插件隔离：`tests/test_architecture.py`。
- 生命周期：`tests/test_lifecycle.py`。
