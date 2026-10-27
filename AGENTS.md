
## 按任务导航

按当前任务选读下列文件及相关章节；涉及架构或行为变更时，先核对架构规范，再读改动文件所属的功能 spec（frontmatter `code:` 列出管辖文件）。

| 阅读目的 | 沿用的文件 |
| --- | --- |
| 项目用途、运行与配置入口 | [README.md](README.md)、[使用手册.md](使用手册.md) |
| 目录职责、依赖方向、资源归属与扩展落点 | [.spec/architecture/spec.md](.spec/architecture/spec.md) |
| 功能行为约定 | `.spec/<id>/spec.md`：messages、delivery、learning、lifecycle、announcements、mention-reply、idiom-chain |
| 拟人化（短句、人格归纳、主动发言）目标与进度 | [docs/拟人化计划.md](docs/拟人化计划.md) |
| RAG 目标、当前进度、下一工作包（暂缓） | [docs/RAG接入计划.md](docs/RAG接入计划.md) |
| 艾特回复未完成事项（验收、未过样本） | [docs/艾特回复质量待办.md](docs/艾特回复质量待办.md) |
| 已结束的计划与评测（Jev 评测、艾特回复改进） | `docs/archive/`，只作历史参考 |

## 功能更新公告

每次完成功能更新，都要同步更新本项目的更新公告；公告配置与发送规则见[使用手册.md](使用手册.md#十七更新修订与多公告队列)。
<!-- workflow:begin -->
workflow 已在本机明确接入；以当前会话 hook 提供的入口读取公共方法和对应角色指引。
无 hook 入口时遵循项目已有约定，不由复制来的管理区推定接入。
首次会话先做项目对齐：结论写入 docs/WORKFLOW-ALIGNMENT.md，差异逐条询问用户，不擅自按任何一方执行。
已有资料：[README.md](README.md)。
<!-- workflow:end -->
