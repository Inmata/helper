---
title: 增量学习
code: wxbot_framework/memory/learning.py
related:
  - wxbot_framework/memory/worker.py
  - tests/test_learning_batch.py
  - tests/test_persistent_memory.py
---

# 增量学习

## 职责

一次学习批次的版本快照、来源校验与原子提交边界。批次选择、唤醒、退避与模型调用
属于 `memory/worker.py`。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 先取版本快照（群 generation 与人物 epoch），锁外调用模型，提交前重新校验。 | 模型输出格式非法或来源不属于本批有效消息：抛 `LLMError`，整批不写。 |
| 2 | 一个批次的知识、来源、mentions、表达习惯（`member_styles`）与游标在**同一外层事务**内提交；任一步失败整批回滚，不推进游标，不留半批结果。 | 写入故障：整批回滚，游标不推进，异常沿调用栈抛出。 |
| 3 | generation 变化（清空或跨天）时整批丢弃；调用方判定失效（如停止）时同样丢弃。 | — |
| 4 | 下层写方法只执行 SQL、不自行提交；嵌套写入加入外层事务，内层异常不得吞掉并提交其余结果。 | — |
| 5 | 单个人物保护（epoch 变化、隔离、manual/forgotten）按条跳过，只影响该条，其余条目仍可接纳并推进游标。 | — |
| 6 | 模型等待、发送节奏等待与线程 join 不占数据库事务锁。 | 连续失败：worker 记录错误类型并退避重试，日志不写模型输出与聊天正文。 |

## 验收

- `tests/test_learning_batch.py`：写入故障整批回滚、外层事务回滚、generation 变化丢弃。
- `tests/test_persistent_memory.py`：无效来源不推进游标、公共记忆证据门槛、
  人物保护逐项处理、表达习惯与样本水位一起提交。
