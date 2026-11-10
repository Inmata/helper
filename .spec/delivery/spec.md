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

## 群图片发送

- 桥接导出 `WxSendImage` 时，后端声明 `group.image.send`。插件经 `ctx.reply_image(message, path)` 回复本地图片，复用发送锁、消息所属连接与发送守卫，不走文本节奏。
- `/表情` 在群内发送随机内置图片；`/表情 吃瓜` 可指定图片。内置表情通过普通图片消息发送。

## 语音条发送

- 通用桥接导出 `WxSendVoice` 时，后端声明 `voice.send`。本地控制台使用 `send-voice 会话ID "音频路径"`；插件可经 `ctx.reply_voice(message, path)` 回复。两者复用现有发送锁及连接校验，不走文本节奏。
- 本地单声道 16 位 PCM WAV 在内存中转成微信 SILK，也可直接发送支持的 SILK 文件。最多 60 秒、输入最多 20 MiB，发送为可播放的原生语音条（类型 34）。发送结果表示微信接受提交。
- `ctx.reply_voice` 也接受内存中的音频；提供语音正文时，只在发送成功后将其记为机器人发言。

## 验收

- `tests/test_text_pacing.py`：按长度随机等待与关闭等待。
- `tests/test_text_routing.py`、`tests/test_framework.py`：会话校验、旧会话拒绝、
  取消与发送 guard。
