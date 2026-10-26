---
title: 启动公告
code:
  - wxbot_framework/app/announcements.py
  - wxbot_framework/feature_config.py
related:
  - wxbot_framework/plugin_engine/manifest.py
  - wxbot_framework/app/maintenance.py
  - tests/test_maintenance.py
  - 使用手册.md
---

# 启动公告

## 职责

按 `maintenance.announcements` 配置在微信连接后向指定群发送更新公告，并记录每次尝试。
用户可见的配置与命令写法见[使用手册第十七节](../../使用手册.md#十七更新修订与多公告队列)。

## 必须成立的规则

| 编号 | 规则 | 失败行为 |
|---|---|---|
| 1 | 配置校验：`id` 非空、不超过 80 字符且唯一；`room` 以 `@chatroom` 结尾；正文不超过 900 UTF-8 字节，加插件贡献署名后仍不超过 900；`announcement` 与 `announcements` 不能同时配置，旧版单对象按一项数组处理。 | 校验失败抛 `ValueError`，启动与 `--check` 报错。 |
| 2 | 多份公告按配置顺序发送；每项本运行只自动尝试一次，同一运行内重连不重复。 | 单项失败或结果未确认不阻断后续公告。 |
| 3 | `startup_notices` 中状态为 `sent` 的公告，下次完整启动自动跳过。 | — |
| 4 | 会话未就绪时暂缓，状态为 `waiting`；连接后再尝试。 | — |
| 5 | 每次尝试先写 `sending` 记录到 `startup_notice_attempts`，结束后更新为 `sent` 或 `unknown`；记录跨重启保留。 | 发送异常或接口未确认成功：状态记 `unknown`，只写日志不抛出。 |
| 6 | 维护者“公告重发”强制发送并追加一条独立尝试记录；配置多份公告时，重发必须指定 ID，状态查询省略 ID 时列出全部。 | 未指定或找不到 ID：抛 `ValueError`。 |
| 7 | 正常启动不调用模型生成公告；`--check` 不发送公告。 | — |

## 验收

- `tests/test_maintenance.py`：队列顺序、已发送跳过、重发记录、多公告 ID 校验与状态。
