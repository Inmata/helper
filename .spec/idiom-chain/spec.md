---
title: 成语接龙
code:
  - plugins/idiom_chain/__init__.py
  - plugins/idiom_chain/judge.py
  - plugins/idiom_chain/lexicon.py
  - plugins/_shared/idiom_state.py
related:
  - plugins/idiom_chain/README.md
  - tests/test_idiom_chain.py
  - tests/test_idiom_lexicon.py
  - tests/test_idiom_ordering.py
---

# 成语接龙

## 职责

全群合作的成语接龙：出题、判定答案、提示与换题、进度持久化。玩法说明见
[插件 README](../../plugins/idiom_chain/README.md)。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 一局一条链，全群共享进度；同一轮先接对的答案生效。 | — |
| 2 | 轮次边界用框架分配的 `dispatch_sequence`：成功发题后保存本群接收水位，水位以内的答案丢弃；不用 `received_at` 或数据库消息 ID 判断轮次。插件重载沿用水位，进程重启使用新序号域。 | — |
| 3 | 直接发送时只有恰好四个汉字且首字等于当前接字才当作答案；艾特时剥掉标签再判定；`/接龙 成语` 对首字、长度、重复错误都给说明。 | 首字正确但非成语：提示失败并保留当前题。 |
| 4 | 每局共享 `接龙提示` 2 次、`接龙换题` 1 次；无合适题目时换题不扣次数。 | — |
| 5 | 机器人出词先要求末字至少 3 个未用常用接法，逐级放宽到 2、1；可持续性相近时常用词与扩展词按 75% / 25% 选择。 | 找不到：消耗一次续接（`bot_rescues` 默认 3，范围 0–10）重新开场，进度保留；续接用完才认输结算。 |
| 6 | 词库内答案本地确认；词库外答案交大模型复核真实性，模型不决定机器人下一词。 | 模型故障：保留当前题和分数，等待重试，不算输赢。 |
| 7 | 成功发送回复后才推进进度或扣除帮助次数。 | 发送失败：保留原状。 |
| 8 | 进度、贡献、目标与剩余帮助写入 `room_states`（功能名 `idiom_chain`），重启、重连、重载后继续。 | — |
| 9 | 配置：`target_rounds` 默认 10（3–30），修改不影响已保存局；`idle_seconds` 默认 1800（60–86400），普通群聊与状态查询不续时。 | 超出范围：加载时抛 `ValueError`。 |

## 验收

- `tests/test_idiom_chain.py`：答案判定、提示与换题次数、续接与认输、持久化、发送失败或模型故障不推进。
- `tests/test_idiom_lexicon.py`：词库与常用接法。
- `tests/test_idiom_ordering.py`：发题水位与轮次边界。
