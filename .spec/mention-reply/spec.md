---
title: 艾特回复
code:
  - plugins/mention_chat/__init__.py
  - wxbot_framework/conversation/dialogue.py
  - wxbot_framework/integrations/llm.py
related:
  - wxbot_framework/conversation/personality.py
  - wxbot_framework/conversation/summary.py
  - tests/test_mention_chat.py
  - tests/test_personality.py
  - tests/test_llm.py
  - tests/test_mention_dedupe.py
  - llm_config.example.json
---

# 艾特回复

## 职责

群里真正艾特机器人时，用本群人格结合聊天上下文与记忆回复发言者。发送约束沿用
[delivery](../delivery/spec.md)。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 只响应真正的艾特：文本消息、已配置群、`is_at_self` 为真且 `at_members` 含本账号；机器人自身消息与控制命令不响应。 | — |
| 2 | 接龙进行中时，去掉艾特标签后为四字且首字等于当前接字的消息交给接龙插件，本插件不回复。 | — |
| 3 | 篇幅由 `Dialogue` 的 SYSTEM 提示词约束：闲聊约 50—120 个汉字，复杂问题未要求详细时约 120—220；明确要求详细时遵从，并使用聊天任务单独配置的超时与输出预算。提问对格式有明确限制（只答一个词、一句或一个选项）时优先满足格式。这是软目标，不做字符裁剪。 | — |
| 4 | 回复按每条 1023 UTF-8 字节减去实际艾特前缀（`@显示名` + ` \n`）的预算用 `split_text` 分段，经 `ctx.reply_at` 顺序发送，不限段数。 | 某段发送失败：抛 `RuntimeError`，停止后续段，不重发已发送段。 |
| 5 | 模型返回 `finish_reason` 为 `length` 或 `content_filter` 时抛 `LLMError`，不发送残文、不自动重试或续写；未知或缺失的结束原因照常使用。 | `LLMError`：guard 仍有效时回复“暂时无法回复：{原因}”。 |
| 6 | 记录或人格状态在生成期间变更时取消本次回复。 | `SnapshotExpired`：只记日志，不发送。 |
| 7 | 同一发言者在本群对同一提问的回复尚在生成时，再次艾特的相同提问不另行生成。 | — |
| 8 | SYSTEM 提示词声明能力边界（只聊天回复，不修改档案、不设提醒、不自称其他模型）、输入分区来源（只有【当前提问】是用户发言）、背景资料不等于当天事件。 | — |

## 验收

- `tests/test_mention_chat.py`：接龙让行、长回复不截断、篇幅软约定。
- `tests/test_personality.py`：篇幅提示只定义一次；分段字节预算、长显示名、中途发送失败或上下文失效停止后续段。
- `tests/test_llm.py`：`content_filter` 残文报错不重试，其他结束原因照常返回；聊天任务的超时与 `max_tokens` 按配置传递，未配置时沿用全局值。
- `tests/test_mention_dedupe.py`：同一提问生成中再次艾特不另行生成。
- `tests/test_mention_chat.py`：背景资料带记录日期；SYSTEM 含能力边界与分区来源说明且各只出现一次。
- `tests/test_personality.py`：敏感真实事件与梗重复规则只定义一次。
