---
title: 主动发言
code:
  - plugins/proactive_chat/__init__.py
  - wxbot_framework/conversation/planner.py
  - wxbot_framework/memory/hourly.py
  - wxbot_framework/plugin_engine/context.py
related:
  - wxbot_framework/conversation/members.py
  - wxbot_framework/integrations/jev.py
  - wxbot_framework/conversation/utterance.py
  - wxbot_framework/feature_config.py
  - tools/evaluate_proactive_jev.py
  - tools/humanlike_metrics.py
  - tests/test_proactive_chat.py
  - tests/test_persistent_memory.py
---

# 主动发言

## 职责

没人艾特机器人时，群里出现停顿就判断要不要像群友一样插一句（事件驱动，`plugins/proactive_chat`）；
定时候选（`memory/hourly.py`）只在冷场时兜底。艾特回复见 [mention-reply](../mention-reply/spec.md)，
发送约束沿用 [delivery](../delivery/spec.md)。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 只有配置 `plugins.proactive_chat.enabled` 显式为 `true` 才启动计时线程 `ProactiveChat.timer`；缺省静默。只处理已配置群（可再用 `rooms` 收窄）的群友文本；机器人自身消息、控制命令不入队；艾特机器人的消息清掉本群待判断项，交给艾特回复，且不占主动配额。 | 配置值越界：`ValueError`，启动与 `--check` 报错。 |
| 2 | 去抖：本群最后一条消息后静默 `quiet_seconds`（默认 3），或首条起满 `max_wait_seconds`（默认 8）才判断一次。每条群友新消息（含艾特）递增本群版本；判断、生成之后核对版本和卸载标志。记录水位取触发这一轮的消息自己的序号（`members.message_id(message)`；消息尚未落库则这一轮放弃），而不是判断开始时的最大序号：水位之后落库的消息——包括已落库但尚未分发给插件、版本还没变的——都算新消息。调用 Jev 之前先查一次，已有更新的消息就不判断；每段发送守卫内（持聊天记录锁、原生发送前）再核对版本、卸载标志与同一水位。落库与守卫共用这把锁。卸载时置停止标志并拿一次这把锁。变了就作废这一轮、等下一次停顿。 | 守卫内发现变化：`SnapshotExpired`，不发送。 |
| 3 | 决策只经 `Planner.decide(context) -> Action`，类型为 `reply` / `wait` / `skip`；当前实现 `JevSpeakPlanner` 用一道 Jev choice 题（选项与问法同 `tools/evaluate_proactive_jev.py` 的 choice_reasons 变体），reply 概率为各 `reply_*` 选项之和，阈值 0.6，连续发言每次加 0.1。只有 `reply` 会生成与发送。Jev 输入里群友匿名为 P1、P2…，机器人昵称换成“机器人”，其他 @昵称换成“@群友”。`JevClient.choice` 要求声明的选项全部给出、概率之和在 1±0.05 内。 | Jev 超时、失败或格式错误（缺选项、总和异常）：`skip`，不重试，不发送。 |
| 4 | 预算 `SpeakBudget` 挂在成员库上（`MemberStore.speak_budget`），事件驱动与定时主动发言共用：每群 10 分钟内至多 5 次（拆分发送算 1 次），两次至少间隔 60 秒，连续主动发言至多 2 次（之后群友累计 3 条新消息才清零）。调用 Jev 前 `check`；发送前 `reserve` 原子地检查并占位（同群同时只有一个主动发言在发，另一路得到 `busy`），首条原生发送正常返回后，`mark_sent` 在发送守卫内、仍持聊天记录锁时记一次（落库失败也照记；原生调用返回假值也记，偏保守），结束 `release`（补记逻辑保留）。群友消息由 `MemberStore.record` 在同一把聊天记录锁内与落库一并计入（与插件是否启用无关），因此守卫内发送期间到达的群友消息排在 `mark_sent` 之后，不会被它清零。锁顺序固定为“数据库锁 → 预算锁”，`SpeakBudget` 内部不得拿数据库锁。本群接龙进行中、北京时间不在 `start_hour`–`end_hour`、或最后一条是机器人自己时不判断。计数只在内存中，重启清零。 | 定时候选被预算拒绝：记 `filtered`/`budget-<原因>`，不占用话题。 |
| 5 | 生成用本群人格（`ctx.llm_for`），返回 `{"speak","text"}`：不艾特、≤30 字目标、超过 60 字或含 @、控制字符时视为不说；`speak=false` 或为空时不发送、不计配额。 | `LLMError`：只记日志，不发送、不回复错误提示。 |
| 6 | 发送统一经 `utterance.speak` 闲聊档；每条经 `ctx.response_guard`（聊天快照与人格快照）并入插话检测；成功发出至少一条才计入配额。快照在读取上下文之前取得，判断期间 `/清空`、跨天或人格切换时拒发。 | `SnapshotExpired`：只记日志，不发送。 |
| 7 | 定时候选 `hourly_chat.quiet_minutes`（默认 0 关闭）大于 0 时，候选时刻前这么多分钟内本群有任何消息（含机器人）就记为 `filtered`/`not-quiet`，不调用模型。 | — |
| 8 | `PluginContext.jev` 按需从大模型配置文件构造 `JevClient`（`jev` 段优先，缺省取主模型配置），只读配置、不请求接口。 | 未配置大模型文件：`ValueError`。 |

## 验收

- `tests/test_proactive_chat.py`：Action 校验；reply 概率求和、连续发言抬阈值、Jev 失败 skip；输入匿名与评测同构；配额、间隔、窗口、连续清零、占位互斥；生成拒绝格式；Jev choice 解析（缺项、总和异常）；插件去抖、艾特与命令不入队、判断/生成期间新消息或艾特作废、守卫内最后一刻新消息拒发、水位建立之前已落库但未分发的消息在判断前（不调 Jev）与守卫内都被拒、触发消息未落库则放弃、守卫内发送期间到达的群友消息不被 `mark_sent` 清零、拆分中途异常照记配额、卸载后不发、缺省不启动、配置校验。
- `tests/test_persistent_memory.py`：`quiet_minutes` 未冷场时 `not-quiet`、冷场后照常发送；定时候选与事件驱动共用预算（占位时 `budget-busy`、间隔内被拒、发出后计数）、原生发送期间到达的群友消息排在 `mark_sent` 之后计入。
- `tests/test_utterance.py`：`interjection_probe(baseline=...)` 显式水位覆盖 `latest_id`。
- `tools/evaluate_proactive_jev.py report`：holdout 误发言率 ≤5%、召回 ≥60%。
- 群内实测：`tools/humanlike_metrics.py --since <开始时刻>` 与日志 `主动发言判断` 行，10 分钟主动发言不超过 5 次。
