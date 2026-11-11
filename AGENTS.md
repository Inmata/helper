
## 按任务导航

按当前任务选读下列文件及相关章节；涉及架构或行为变更时，先核对架构规范，再读改动文件所属的功能 spec（frontmatter `code:` 列出管辖文件）。

| 阅读目的 | 沿用的文件 |
| --- | --- |
| 项目用途、运行与配置入口 | [README.md](README.md)、[使用手册.md](使用手册.md) |
| 目录职责、依赖方向、资源归属与扩展落点 | [.spec/architecture/spec.md](.spec/architecture/spec.md) |
| 功能行为约定 | `.spec/<id>/spec.md`：messages、delivery、learning、lifecycle、announcements、mention-reply、proactive-chat、idiom-chain |
| 拟人化（短句、人格归纳、主动发言）目标与进度 | [docs/拟人化计划.md](docs/拟人化计划.md) |
| RAG 目标、当前进度、下一工作包（暂缓） | [docs/RAG接入计划.md](docs/RAG接入计划.md) |
| 艾特回复未完成事项（验收、未过样本） | [docs/艾特回复质量待办.md](docs/艾特回复质量待办.md) |
| 群聊审计修复的范围、实施与实测记录 | [docs/群聊审计修复计划.md](docs/群聊审计修复计划.md) |
| 已结束的计划与评测（Jev 评测、艾特回复改进） | `docs/archive/`，只作历史参考 |

## 功能更新交付流程

功能完成并通过必要检查、准备好本次更新公告后，固定按以下顺序完成交付：

1. **重启**：用 `bot.cmd restart` 正常重启整个机器人，等待微信重新连接。
2. **发公告**：通过现有公告队列发送本次公告，确认对应公告 ID 的发送状态为 `sent`。
3. **提交**：将本次相关代码、测试与文档提交到 Git。
4. **推送**：推送当前分支到 `origin`，确认推送成功。

以上步骤默认直接执行，无需逐项再次确认；任一步失败先修复再继续，有外部硬阻塞时说明具体阻塞点。公告配置与发送规则见[使用手册.md](使用手册.md#十七更新修订与多公告队列)。
