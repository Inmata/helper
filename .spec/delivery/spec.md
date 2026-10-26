---
title: 文本发送
code: wxbot_framework/messaging/delivery.py
related:
  - wxbot_framework/messaging/pacing.py
  - tests/test_text_pacing.py
  - tests/test_text_routing.py
  - tests/test_framework.py
---

# 文本发送

## 职责

插件与后台任务唯一的发送入口：序列化发送、连接切换保护、发送节奏与取消。
发送内容的生成不在本规范内。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 发送前校验正文：UTF-8 编码后 1–1023 字节，且不含 NUL。 | — |
| 2 | 发送前检查连接与上下文：`expected_session` 与当前后端会话不一致时拒绝，报“消息所属的微信连接已结束”，不发出旧会话回复。 | 未连接或连接已切换：抛 `RuntimeError`，调用方据此放弃该条回复。 |
| 3 | `delivery_guard` 在发送前再次判定；判定失败时取消本次发送（清空、跨天、人工资料修订与连接切换都会让旧回复失效）。 | — |
| 4 | 等待（发送节奏）不占连接锁：状态查询、断线清理与退出可以及时进行。 | — |
| 5 | 发送锁覆盖等待与原生发送，多个插件不会同时等待后集中发送。 | — |
| 6 | 需要真实艾特而当前后端不支持时，抛 `CapabilityError`，不降级为普通文本。 | 后端返回失败：按后端语义返回，不重复发送。 |

## 验收

- `tests/test_text_pacing.py`：按长度随机等待与关闭等待。
- `tests/test_text_routing.py`、`tests/test_framework.py`：会话校验、旧会话拒绝、
  取消与发送 guard。
