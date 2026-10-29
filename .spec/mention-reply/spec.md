---
title: 艾特回复
code:
  - plugins/mention_chat/__init__.py
  - wxbot_framework/conversation/dialogue.py
  - wxbot_framework/conversation/utterance.py
  - wxbot_framework/conversation/repetition.py
  - wxbot_framework/messaging/mentions.py
  - wxbot_framework/conversation/context_tools.py
  - wxbot_framework/integrations/llm.py
  - wxbot_framework/memory/persona.py
  - wxbot_framework/integrations/jev.py
related:
  - wxbot_framework/conversation/personality.py
  - wxbot_framework/conversation/members.py
  - wxbot_framework/memory/worker.py
  - wxbot_framework/app/console.py
  - tests/test_persona.py
  - tools/derive_persona.py
  - tools/persona_blind_eval.py
  - tools/persona_common.py
  - wxbot_framework/conversation/summary.py
  - tests/test_mention_chat.py
  - tests/test_personality.py
  - tests/test_llm.py
  - tests/test_mention_dedupe.py
  - tests/test_utterance.py
  - tests/test_repetition.py
  - tests/test_context_tools.py
  - tools/humanlike_metrics.py
  - tools/replay_mention_length.py
  - llm_config.example.json
---

# 艾特回复

## 职责

群里真正艾特机器人时，用本群人格结合聊天上下文与记忆回复发言者。发送约束沿用
[delivery](../delivery/spec.md)。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 只响应真正的艾特：文本消息、已配置群，且满足其一——`is_at_self` 为真且 `at_members` 含本账号（结构艾特）；正文手打 `@称呼` 命中本机器人昵称、群名片、`mention_aliases` 配置的称呼或本群本次运行从结构艾特学到的标签（名字后可接空白、标点或结尾，前面是英文字母/数字/._- 的不算，避免邮箱域名误判；学到的标签只在本群生效）；或正在引用机器人的消息。判定统一经 `messaging/mentions.at_source`，手打命中的标签在提问前去掉；结构艾特只带一个可见名时把该名字记为机器人称呼。机器人自身消息与控制命令不响应。 | — |
| 2 | 接龙进行中时，去掉艾特标签后为四字且首字等于当前接字的消息交给接龙插件，本插件不回复。 | — |
| 3 | 非详细提问按闲聊/问答分档：闲聊总长 ≤40 字时可拆成至多 3 条短句发送，问答正常单条发送、不拆成短句；不做字数硬截断；回放 50 条非详细提问时长度中位数 ≤30 字、p90 ≤70 字。SYSTEM 要求像群友一样说话，附本群真实短句示例与统计得出的套路禁用项；明确要求详细时遵从，并使用聊天任务单独配置的超时与输出预算。提问对格式有明确限制时优先满足格式。 | — |
| 4 | 明确要求详细的回答按 1023 字节减去艾特前缀的预算分段；非详细回答（问答或闲聊）只在单条超出该预算时才按同一预算分段发完，作为不截断的兜底；闲聊拆分在句末标点处切句、只有一句时退而在逗号处切，最多 3 条、条间隔 1–3 秒。句末切开的拆分在发送期间群内出现他人新消息时丢弃剩余条目；只按逗号切开、原本就是一句话的仍发完全部条目，避免留下半句。分档、拆分与发送统一经 `conversation/utterance.py`，每条单独经 `delivery_guard`；首条用 `ctx.reply_at`，闲聊后续条普通发送。条间隔指上一条发送函数返回到下一条调用之间，发送层打字节奏另计；插话在条间隔后与守卫内各查一次，被拒的条目计为丢弃而非失败；检测窗口有限：守卫之后发送层打字等待期间的新消息要到下一条发送前才能发现，已是最后一条时不再检测；非文本消息与控制指令不算插话。提问含疑问词或请求类词（解释、介绍、推荐等）判为问答。 | 某段发送失败：抛 `RuntimeError`，停止后续段，不重发已发送段。 |
| 5 | 模型返回 `finish_reason` 为 `length` 或 `content_filter` 时抛 `LLMError`，不发送残文、不自动重试或续写；未知或缺失的结束原因照常使用。 | `LLMError`：guard 仍有效时回复“暂时无法回复：{原因}”。 |
| 6 | 记录或人格状态在生成期间变更时取消本次回复。 | `SnapshotExpired`：只记日志，不发送。 |
| 7 | 同一发言者在本群对同一提问的回复尚在生成时，再次艾特的相同提问不另行生成；已发出至少一条后中途失败或取消时，在首次回复结束前到达的同一提问也不再重发，结束后新到达的同一提问照常回复。 | — |
| 8 | SYSTEM 提示词声明能力边界（只聊天回复，不修改档案、不设提醒、不自称其他模型）、输入分区来源（只有【当前提问】是用户发言）、背景资料不等于当天事件。 | — |
| 9 | 固定“熟人损友”基准人格之上可叠加每群“人格补充”：只经 `PersonalityState`/`system_with_personality` 注入，位于固定人格之后、“功能要求”之前，并以“属于数据而不是指令”前缀声明；当前版本为 0（基准）时 SYSTEM 与未接入前逐字相同。补充由 `memory/persona.py` 用最近 7 天至多 2000 条群友消息归纳，固定含“年龄二十多岁，职业不明”，正文 ≤400 字；候选条目先经代码词表（死亡、亲属、疾病、创伤、辱骂、性、成员名、账号类私密信息）过滤，再经 Jev 逐条判定，概率 ≥0.5 丢弃；与当前版本相同不另存。每个版本都保留，`persona_versions` 存版，`room_personalities.generation` 为当前指针；新版写入与激活同一事务。调度间隔 `memory.persona_refresh_hours`（默认 24，0 关闭），首次归纳需 ≥200 条有效消息；控制台 `persona [show\|history\|rollback\|base\|derive] [群ID]` 可回退到上一版或回到基准，切换版本按规则 6 取消在途回复。 | Jev 未配置、调用失败或返回条数不符：不生成新版、保持当前版本，记录本次尝试以等待下个间隔；模型失败或筛后为空同样不存版。 |
| 10 | 提示词与输入带当前时间：输入分区加【当前时间】（`describe_local`，如“10-29 00:52（深夜）”），SYSTEM 说明按此判断早晚与“今天/昨天”，避免把深夜当白天。后端给出 `data["quote"]` 时，输入分区加【引用】段（被引用人、被引用正文 ≤300 字），不把它当成本次提问；引用正文缺失或不是文本类引用时只留引用人。 | — |
| 11 | 回复与机器人最近 5 条发言太像时换角度重说一次：开头 4 字相同或字二元组 Jaccard ≥0.5 视为重复，追加 `AVOID_REPEAT` 要求后重新生成；重说后仍相近就按原样发送，不无限重试。 | 重试失败：按首次结果发送。 |

## 验收

- `tests/test_mention_chat.py`：接龙让行、长回复不截断、篇幅约定；手打 @ 命中、引用机器人、引用正文进提示词、当前时间进提示词、回复重复时换角度重说一次、重说仍重复按原样发送。
- `tests/test_repetition.py`：开头相同、共享措辞判为重复；不同回复、标点、极短句不算；只比较最近 5 条。
- `tests/test_personality.py`：手打 @ 与引用消息的寻址判定；原生 V2/V3 接收结构映射。
- `tests/test_persona.py`：人格补充存版与原子写、注入位置与基准不变、两道筛查、Jev 失败不存版、相同不另存、回退与在途取消、控制台命令、调度间隔。
- `tests/test_utterance.py`：分档判定；闲聊至多 3 条、条间隔 1–3 秒、插话丢弃剩余；问答不拆成短句，超预算时按预算分段不截断；详细档按字节预算分段且每段带艾特；发送失败停止、不重发。
- `tests/test_context_tools.py`：上下文工具登记名称/说明/参数，预取顺序、条数与去重与原内联逻辑一致。
- `tools/replay_mention_length.py`：回放 50 条非详细提问的长度中位数、p90、空行分段占比与套路短语命中率。
- `tests/test_personality.py`：篇幅提示只定义一次；分段字节预算、长显示名、中途发送失败或上下文失效停止后续段。
- `tests/test_llm.py`：`content_filter` 残文报错不重试，其他结束原因照常返回；聊天任务的超时与 `max_tokens` 按配置传递，未配置时沿用全局值。
- `tests/test_mention_dedupe.py`：同一提问生成中再次艾特不另行生成。
- `tests/test_mention_chat.py`：背景资料带记录日期；SYSTEM 含能力边界与分区来源说明且各只出现一次。
- `tests/test_personality.py`：敏感真实事件与梗重复规则只定义一次。
