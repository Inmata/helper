<!-- workflow-alignment:begin -->
# 项目对齐

## 对齐依据

- 生成时间：2026-10-26；workflow 0.5.1；基准提交 `f035630`。同日按裁决重组 spec 与文档，提交 `685f57e`：删除 `.spec/project/` 与 `架构与阅读指南.md`，原文见父提交 `f035630`。
- 已读文件（大小 B / mtimeMs）：

| 路径 | 大小 | mtimeMs | 读取范围 |
| --- | --- | --- | --- |
| AGENTS.md | 1458 | 1792998948214 | 全文 |
| README.md | 5780 | 1792998944207 | 全文 |
| 使用手册.md | 36944 | 1792636669516 | 标题 + 第十七节 |
| .spec/architecture/spec.md | 4953 | 1792998854867 | 全文 |
| .spec/{messages,learning,delivery,lifecycle}/spec.md | 2002/1856/1631/2578 | 1792998865513～521 | 全文 |
| .spec/{announcements,mention-reply,idiom-chain}/spec.md | 1958/2122/2525 | 1792998878313～922703 | 全文 |
| docs/RAG接入计划.md | 19994 | 1792547785346 | 全文 |
| docs/Jev中文相关性评测.md | 13730 | 1792547878161 | 标题 |
| plugins/idiom_chain/README.md | — | — | 全文 |
| start_bot.ps1 | 1275 | 1792118903917 | 全文 |
| requirements.txt | 32 | 1792380137201 | 全文 |
| tools/check_publication.py | — | — | 开头与参数 |

- 未找到：CLAUDE.md、CONTRIBUTING.md、package.json / pyproject.toml / Makefile、CI 配置、`.spec/design/`。
- 扫描噪声：`.runtime/`（已被 .gitignore 忽略）含旧审查副本 `review-pr1/` 与 RAG 评测原始响应，进入了扫描索引，但不是项目资料。

## 文档索引

| 资料 | 职责 | 何时必读 |
| --- | --- | --- |
| AGENTS.md | 按任务导航、功能更新公告规则 | 每次会话 |
| README.md | 功能、运行环境、首次配置、目录 | 运行或配置相关 |
| 使用手册.md | 群内指令、维护命令、公告配置（第十七节）、fork 维护（第十八节） | 用户可见行为、公告 |
| .spec/architecture/spec.md | 边界、目录职责、依赖方向、资源归属、线程、扩展落点；整体结构检查基准 | 架构变更、新增模块或依赖 |
| .spec/{messages,delivery,learning,lifecycle}/spec.md | 消息接收、文本发送、增量学习、启动与退出 | 改对应 `code:` 文件 |
| .spec/{announcements,mention-reply,idiom-chain}/spec.md | 启动公告、艾特回复、成语接龙 | 改对应 `code:` 文件 |
| docs/RAG接入计划.md | RAG 方案、工作包 0～12、验收；当前仅步骤 0 完成 | RAG 相关任务 |
| docs/Jev中文相关性评测.md | Jev 小样本结果与局限 | 调 Jev 阈值或复跑评测 |
| docs/艾特回复质量待办.md | 艾特回复已知质量反例（complex06/09、normal07/10）与 2026-10-22 评测局限 | 改艾特回复提示词或质量 |
| docs/艾特回复质量改进计划.md | 10-22—10-26 真实群聊复盘缺陷 D1—D10、工作包与 spec 影响；已实施、审查、评测并部署，真实群效果待观察 | 实施或复核艾特回复质量改进 |
| plugins/*/README.md | 各插件说明 | 改对应插件 |

## 全量文档清单

| 路径 | 分类 | 是否已读 |
| --- | --- | --- |
| AGENTS.md | 约束 | 已读 |
| README.md | 产品 | 已读 |
| 使用手册.md | 使用 | 部分已读 |
| THIRD_PARTY_NOTICES.md | 发布 | 存在但未读，按需查阅 |
| .spec/*/spec.md（8 个） | 产品 | 已读 |
| docs/RAG接入计划.md | 计划 | 已读 |
| docs/Jev中文相关性评测.md | 计划 | 只读标题 |
| docs/艾特回复质量待办.md | 计划 | 已读 |
| docs/艾特回复质量改进计划.md | 计划 | 已读（本会话编写） |
| docs/diagrams/*.mmd | 产品 | 存在但未读（PNG 为准） |
| native/README.md | 产品 | 存在但未读，按需查阅 |
| plugins/idiom_chain/README.md | 产品 | 已读 |
| plugins/{chat_summary,member_identity,mention_chat,personality}/README.md | 产品 | 存在但未读，按需查阅 |
| plugins/idiom_chain/data/README.md、LICENSE.chinese-xinhua.txt | 发布 | 存在但未读 |
| native/build_bridges.ps1、start_bot.ps1 | 工具 | start_bot.ps1 已读 |
| .runtime/**（忽略目录，含 review-pr1 副本与评测响应） | 归档 | 不读 |

## 验收入口

| 用途 | 命令 | 环境要求 |
| --- | --- | --- |
| 全量离线测试 | `python -B -m unittest discover -s tests` | 64 位 Python（已验证 3.13） |
| 入口检查 | `python -B main.py --config <合成配置> --check` | 不建库、不注入微信、不请求模型 |
| 插件清单 | `python -B main.py --config <合成配置> --list-plugins` | 同上 |
| 依赖边界 | `tests/test_architecture.py`（AST 静态检查） | 含在全量测试中 |
| workflow spec 漂移 | `workflow spec check --project <根>` | 需 git；工作区管辖代码与 spec 同步修改可通过，提交时需一并包含 |
| 发布前检查 | `python tools/check_publication.py [--private-markers-file <文件>]` | 只报告问题类型，不输出密钥 |
| 启动 | `start_bot.ps1 [-RunSeconds N] [-Python <路径>]` | 需微信 4.1.15.13 登录；真实环境 |
| 原生桥接构建 | `native/build_bridges.ps1` | 未读，需 MSVC（推断） |

## 目录与命名

- 框架代码在 `wxbot_framework/`（app/conversation/memory/messaging/plugin_engine/storage/integrations），插件在 `plugins/<名称>/`，目录名即插件名。
- 私有配置 `bot_config.json`、`members_config.json`、`llm_config.json` 不入库，模板为 `*.example.json`；运行数据在 `.runtime/`，日志在 `logs/`，备份在 `backups/`（均忽略）。
- spec 按 workflow 布局：`.spec/<id>/spec.md`，目录名即 id；`code:` 只列具体被管辖文件，测试与相关实现放 `related:`；架构基准 `.spec/architecture/spec.md`（无 `code:`）。不另写阅读指南类文档。
- 提交信息为中文 Conventional Commits（`feat:`/`fix:`/`docs:`/`refactor:`/`chore:`/`release:`）。
- 图片以 `docs/images/*.png` 为准，Mermaid 源在 `docs/diagrams/*.mmd`。

## 术语

| 术语 | 含义 |
| --- | --- |
| 原生桥接 | `native/` 下的 DLL，负责微信文本收发 |
| capture / 接收水位 | 消息落库前先登记的整数水位，用于接龙等轮次边界 |
| guard | 发送前的会话有效性校验；清空、跨天、重连后旧回复被取消 |
| 公告队列 | `maintenance.announcements`，每次完整启动尝试发送未成功的公告 |
| 修订（upgrade_file） | 一次性本地人物纠错文件，按账号与 ID 记账 |
| Jev / Noul | TypeSafe 判断模型；Noul 是“答案为是”的概率，不是相关程度 |

## 待裁决差异

| ID | 项目现行做法 | workflow 默认 | 影响 | 我的建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| D1 | 功能 spec 嵌套在 `.spec/project/<主题>/spec.md` | 新 spec 放 `.spec/<id>/spec.md`，目录名即 id | 两种路径并存会让 spec 查找和 `code:` 归属分散；`spec check` 当前对嵌套布局返回无发现 | 沿用项目嵌套布局，新功能 spec 也放 `.spec/project/<id>/` | accepted-workflow |
| D2 | 架构约定写在根规范 `.spec/project/spec.md` 与 `架构与阅读指南.md` | 整体结构检查对照 `.spec/architecture/spec.md` | 不建 architecture spec 时，结构检查需改为对照根规范 | 不新建，结构检查以 `.spec/project/spec.md` 为架构基准 | accepted-workflow |
| D3 | 计划文档（如 `docs/RAG接入计划.md`）没有“spec 影响”一节 | 计划末尾必须有“spec 影响”，确认计划即批准其中 spec 改动 | 没有这一节时，每次实施改 spec 都要单独问用户 | 后续新计划加该节；RAG 计划在开始实施步骤 1 前补上 | accepted-workflow |
| D4 | `架构与阅读指南.md` 第四、八节写入单次修复的证据、本机临时证据路径、审查任务 ID 和已知待办 | 进度与证据回原计划或提交记录；长期说明只写已实现行为 | 架构指南混入一次性证据，时间一长会过期（已出现“尚未提交”过期措辞） | 架构指南只保留行为说明，证据与待办移到提交说明或单独记录 | accepted-workflow |

## 结构建议

| ID | 问题 | 位置 | 建议改法 | 状态 |
| --- | --- | --- | --- | --- |
| F1 | “适用于本次尚未提交的长回复修复工作区”已过期，该修复已于 `6243c4e` 提交 | 架构与阅读指南.md 第四节 | 删去“尚未提交”限定，改为指向提交 `6243c4e`；随 D4 决定一并处理 | accepted-change |
| F2 | 目录表没有列出 `docs/`（计划、评测、图片） | README.md 目录表、架构与阅读指南.md 第二节 | 各补一行 `docs/`：计划、评测与架构配图 | accepted-change |
| F3 | 公告队列有用户可见行为和验收，且多次修订（`7993c49`、`c081f4e`、`8b371f6`），无 spec | 使用手册.md 第十七节 | 建 spec `announcements`；来源：使用手册第十七节；`code:` wxbot_framework/app/announcements.py、tests/test_maintenance.py | accepted-change |
| F4 | 艾特回复篇幅、分段发送与截断报错多次修订（`a092212`、`6243c4e`），规则只写在架构指南 | 架构与阅读指南.md 第四节 | 建 spec `mention-reply`；`code:` plugins/mention_chat/__init__.py、wxbot_framework/conversation/dialogue.py、wxbot_framework/integrations/llm.py；related: tests/test_mention_chat.py、tests/test_llm.py | accepted-change |
| F5 | 成语接龙规则复杂且多次修订（`154918c`、`f0238ee`、`d360e3f` 等），无 spec | plugins/idiom_chain/README.md | 建 spec `idiom-chain`；`code:` plugins/idiom_chain/__init__.py、judge.py、lexicon.py；related: tests/test_idiom_*.py | accepted-change |

## 已裁决记录

D1 accepted-workflow：用户：全按照 workflow 的规则来，之前的也重写。现有 4 个子 spec 迁到 .spec/<id>/spec.md；复核条件：workflow 改变 spec 布局规则
D2 accepted-workflow：用户：按 workflow 的来，之前的不要了删了就行。架构约定改放 .spec/architecture/spec.md，删除 .spec/project/；复核条件：同上
D3 accepted-workflow：用户采用建议：新计划末尾加“spec 影响”，RAG 计划实施步骤 1 前补上
D4 accepted-workflow：用户采用建议：架构指南只留行为，修复证据与待办迁出到提交说明或单独记录
F1 accepted-change：用户批准，2026-10-26 本会话执行：架构与阅读指南.md 整体删除（用户：这种阅读指南会增加负担），过期措辞随之消失；行为并入 .spec/architecture 与 .spec/mention-reply，证据与待办不进 spec
F2 accepted-change：用户批准，2026-10-26 本会话执行：README 目录表补 docs/ 与 .spec/ 行，指南链接改指 .spec/architecture/spec.md
F3 accepted-change：用户批准，2026-10-26 本会话执行：新建 .spec/announcements/spec.md，code: announcements.py、feature_config.py；测试移到 related
F4 accepted-change：用户批准，2026-10-26 本会话执行：新建 .spec/mention-reply/spec.md，code: mention_chat、dialogue.py、llm.py；测试放 related
F5 accepted-change：用户批准，2026-10-26 本会话执行：新建 .spec/idiom-chain/spec.md，code 含 _shared/idiom_state.py；测试放 related
<!-- workflow-alignment:end -->
