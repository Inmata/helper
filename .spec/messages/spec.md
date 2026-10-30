---
title: 消息接收
code:
  - wxbot_framework/app/ingress.py
  - wxbot_framework/image_receiver.py
  - wxbot_framework/image_metadata.py
  - wxbot_framework/images.py
  - wxbot_framework/integrations/wechat.py
  - native/text_service.py
related:
  - wxbot_framework/plugin_engine/manager.py
  - tests/test_idiom_ordering.py
  - tests/test_framework.py
  - tests/test_image_receiver.py
  - tests/test_image_native.py
  - tests/test_image_metadata.py
  - wxbot_framework/recent_images.py
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

## 图片消息（type = 3）

图片事件走与文本相同的分发顺序。原生服务解析图片 XML 后交给账号专属的异步接收器，
下载完成才把事件投递给插件；插件不需要接触 XML、CDN 或原生下载接口。

| 字段 | 含义 |
|---|---|
| `type` | `3`，`Message.is_image` 为真 |
| `text` / `msg` / `content` | 空字符串；图片没有可展示正文 |
| `image_status` | 投递时为 `ready`、`failed` 或 `skipped` |
| `image_path` | `ready` 时为已保存图片的本地绝对路径，其余为空 |
| `image_error` | `failed` / `skipped` 的原因；不包含 XML、CDN 地址或密钥 |
| `image_quality` | `standard`、`original`、`thumbnail` 或 `unknown` |
| `image_metadata` | 公开字段：`md5`、`width`、`height`、`length`、`hdlength`、`cdnthumbwidth`、`cdnthumbheight`、`cdnthumblength`、`cdnmidwidth`、`cdnmidheight`、`encryver` |

状态语义：

- `ready`：图片已下载、校验并保存在当前账号目录，`image_path` 可直接读取。
- `failed`：下载、校验或保存失败，`image_path` 为空；原事件不重试，可观察原因在 `image_error`。
- `skipped`：不在支持范围，未发起下载。包括会话不在图片名单、消息只有缩略图引用（`cdnthumbaeskey` 不被原生下载接口接受）、以及密钥格式不符合原生接口要求。

公开边界：

- 原始图片 XML（`raw_content`）只在解析和下载请求内部使用，解析后立即从事件中移除；插件拿不到 XML、CDN 地址、`filekey` 或密钥。
- 图片事件只在已编译桥接同时导出 V4 媒体队列与 `WxDownloadImage` 时产生；否则后端不声明图片能力，依赖它的插件不会启用。
- 只处理启动后实时到达的图片，不补抓历史；接收层不识别图片内容，识别只在艾特回复里按需进行（见 [mention-reply](../mention-reply/spec.md) 规则 12）。

最近图片索引（`wxbot_framework/recent_images.py`）：

- 原生服务在图片进入下载队列时记一条 `pending`（带原消息编号、发送者与图片 md5），下载结束后更新为 `ready` / `failed` / `skipped`；写入发生在插件分发队列之外，正在生成的艾特回复不会挡住记录。
- 只保存会话、发送者、状态、本地路径、md5 与原消息编号，不保存 XML、CDN 地址或密钥；进程内按群有界（每群 64 条、至多 256 个群），服务启动时清空，插件热重载不影响。
- 记录带“已看过”标记：艾特回复看图成功后标记，之后不再按“刚发的图”选中，按引用仍能找到。按“刚发的图”查找时可选自己发的或别人发的（`from_others`）。
- 引用消息解析出的 `quote` 在被引用的是图片（`ref_type=3`）时额外带 `svrid` 与 `image_md5`，供艾特回复对上索引；不带缩略图 md5、密钥或 CDN 地址。

## 验收

- `tests/test_idiom_ordering.py`：capture 幂等、水位按群记录、原生序号重置与插件重载后的水位。
- `tests/test_framework.py`：allowlist 过滤、维护者私聊分流、插件分发顺序。
- `tests/test_image_receiver.py`：ready/failed/skipped 投递、队列与下载失败可观察、事件不含 XML 与密钥。
- `tests/test_mention_tools.py`：最近图片索引的记录、更新与有界；引用图片字段不泄露密钥或 CDN 地址；入队时记 pending 并带 md5，日志不含路径。
