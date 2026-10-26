---
title: 消息接收
code: wxbot_framework/app/ingress.py
related:
  - wxbot_framework/plugin_engine/manager.py
  - tests/test_idiom_ordering.py
  - tests/test_framework.py
---

# 消息接收

## 职责

把一条收到的消息按固定顺序交给成员库、维护通道与插件。这里不做业务判断，
也不决定回复内容。

![消息主链路：编号 1–8 为一次接收的主要步骤](../../docs/images/message-flow-3.png)

图注：图为从 `bridge` 到 `ServiceFacade` 的主链路；`ingress` 内的 allowlist 过滤、快速上下文清空、维护分流与人格兼容步骤未展开，完整顺序见规则 1。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 顺序固定，逐段可读：维护者私聊分流 → `chat_allowlist` 过滤 → `capture` → 落库与学习通知 → 快速上下文清空 → 群维护分流 → 固定人格旧命令兼容 → 插件分发。 | 维护者私聊命中管理通道：进入维护队列后直接返回，不再分发插件；群维护未命中：继续插件分发，不重复回复。 |
| 2 | `capture` 先于落库：落库慢时，发题水位仍能排除“已经收到但尚未入队”的答案。 | — |
| 3 | 落库失败只记录日志，消息继续分发到插件。 | 落库异常：`logger.exception` 记录，分发继续。 |
| 4 | 同一条消息的所有插件拿到同一份群状态快照；已捕获的空状态不会被后来的游戏状态替换。 | — |
| 5 | 唯一维护者的私聊是管理通道，不要求把该会话加入群收发名单。 | — |
| 6 | 接收水位按群记录：原生序号重置不影响进程内序号，插件重载保留 epoch，进程重启更换 epoch。 | — |

## 验收

- `tests/test_idiom_ordering.py`：capture 幂等、水位按群记录、原生序号重置与插件重载后的水位。
- `tests/test_framework.py`：allowlist 过滤、维护者私聊分流、插件分发顺序。
