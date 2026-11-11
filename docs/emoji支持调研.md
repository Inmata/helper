# 微信回复 emoji 支持调研

调研日期：2026-11-11。范围：比较纯文本里的 Unicode emoji 与微信内置小表情；未发送微信消息。

## 结论

回复以文字和语境为主，表情偶尔点缀，大多数回复仍用纯文字。流汗黄豆 `😅` 是可选表达，不固定优先使用。Unicode emoji 和微信内置小表情代码（如 `[捂脸]`、`[旺柴]`）都沿用文本通道，无需新增图片资源管理。**Windows 微信 4.2 经本项目入口发送后的显示效果待实测。**

用户所说的“苹果原生 emoji”可分为 Unicode 字符和 Apple 的图形呈现。纯文本只携带字符或字符序列；接收端决定字形。Unicode 标准允许不同厂商采用不同设计，`U+FE0F` 只能请求 emoji 呈现，无法指定 Apple 外观。[Unicode Emoji 标准](https://unicode.org/reports/tr51/)、[Unicode 官方 FAQ](https://unicode.org/faq/emoji_dingbats.html)

## 三种形式的区别

| 形式 | 发送内容 | 纯文本方案的工作量 | 显示由谁决定 |
| --- | --- | --- | --- |
| Unicode emoji | `😂`、`😅`、`🤔` 等字符或序列 | 模型直接生成字符，文本通道需保留 Unicode | 接收端软件、字体及其支持版本 |
| 微信内置小表情 | `[捂脸]`、`[旺柴]` 等代码 | 模型生成有效代码；确认客户端解析后可沿用文本通道 | 微信客户端的代码识别与表情资源 |
| 图片／自定义表情包 | 图片文件或独立表情消息 | 需要相应媒体发送能力 | 发送的图像资源及消息类型 |

后两类需要区分：开源 `WeChatExpression` 在文本中匹配方括号代码，再插入对应图像组件；`openwechat` 则将文本回复、图片回复和表情包回复分成不同方法。[方括号解析源码](https://github.com/jy231613/WeChatExpression/blob/master/wx_expression.dart#L263-L309)、[独立消息方法源码](https://github.com/eatmoreapple/openwechat/blob/master/message.go#L163-L189)

## 有源码依据的小表情代码

直接读取了 GitHub 上的 `WeChatExpression` 映射与解析代码，包含 `微笑`、`呲牙`、`偷笑`、`捂脸`、`旺柴`、`吃瓜`、`机智`、`耶`、`OK` 等名称；解析器识别 `[名称]`，查表后使用对应图片资源。[名称与图片映射](https://github.com/jy231613/WeChatExpression/blob/master/wx_expression.dart#L110-L211)

另有一份从微信 Mac 数据目录提取的 `newemoji-config.xml` 转录，直接列出 `[捂脸]`／`[Facepalm]`、`[旺柴]`／`[Doge]`，并映射到 PNG 文件。该资料由个人发布，标注来源为 Mac 客户端，适合作为代码候选依据；Windows 4.2 的最终可用集合仍待实测。[配置来源与内容](https://gist.github.com/cdpath/2a42edb3f2b81d18de2ae9ed8a3ac48b)

## 本项目已追踪的入口

- `native/text_service.py:462–471` 的 `_send_text` 将 `content` 原样交给 `--dispatch`；`native/run_selftest.py:257–259` 将内容编码为 UTF-8，并限制为 1–1023 字节。两种文本形式都可进入当前发送入口；emoji 的字节数会计入限制。
- `wxbot_framework/memory/persona.py` 保留 `EMOJI_MIN_RATE = 0.03`，低频提示为“群里表情用得少，偶尔贴合语境点缀一个即可。”；`assemble` 按条件加入该补充，`system_with_personality` 注入已保存补充。统一人格规则允许偶尔点缀，并要求文字为主、按语境选择；已有低频补充与此兼容，无需迁移历史版本。
- 现有 `/表情` 按 `.spec/delivery/spec.md:32` 发送普通图片消息，属于单独的图片路线。
- 已用现有 `plan_utterance` 离线检查 6 组中文与 emoji／微信表情混排文本，正文均完整保留，并通过 UTF-8 编解码往返检查；未连接微信。

## 建议与待实测项

艾特回复和主动接话共用的固定人格规则已明确：偶尔使用一个贴合语境的小表情，不固定偏爱某一种，不连续几条都加，也不按固定比例添加。认真答疑、安慰时克制，指定原话和格式要求优先。

新的人格归纳继续统计群友用法，低频时提示偶尔点缀。自己生成的文字正文含表情时，模型选择文字形式，保留正文里的表情；生成语音正文时不主动加表情，指定原话沿用原有处理。

后续实测需同时查看发送端 Windows 微信 4.2 和接收端：字符是否完整、代码是否变成内置表情、与中文混排是否正常。提示词引导实际使用频率，本次离线检查未测量群内生成占比。
