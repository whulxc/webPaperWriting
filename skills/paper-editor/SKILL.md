---
name: paper-editor
description: web论文写作（旧称 Chat论文写作，调用名 `paper-editor`）：用户要求整理科研工程为论文材料、持续写作、按版本/语言/图号取件、图件/PDF审阅或同步交付时使用。按真实宿主分工：Codex 初始化与维护；ChatGPT 通过所选 WebCodex 编辑和审阅。同一项目契约，不猜项目身份、不自动换绑 Tunnel。
---

# web论文写作

技能基线版本 **0.9.11**，共享契约 **paper-editor.project.v1**。本目录是 webPaperWriting V0.8.1 的公开分发源，使用 `paper-skill` 生成技能包或安装目录，并按文件哈希核对。已安装副本有用户修改时先审查差异。技能、软件、可选规范库和论文修订分别记录版本。项目授权由实际宿主和连接配置决定。

软件及工作流的英文名为 `webPaperWriting`，配套技能显示名为“web论文写作”，兼容用户沿用的“Chat论文写作”称呼。技能调用名 `paper-editor`、目录名、`paper-editor.project.v1` 契约和既有软件标识继续兼容。实际项目、Drive 目录与旧回执按其真实身份读取。

## 先读取连接的文件交付设置

文件展示、视觉审阅和附件请求先遵循[文件交付策略](references/file-delivery-policy.md)。通过当前项目 `paper_capabilities` 返回的顶层 `chat_file_delivery` 核对设置。`drive_links_only` 优先于下文及其他参考中的附件、分片和预览调用说明。本文其余部分按这个设置选择允许的路径。

受管稿件定向查审阅链接可用 `paper_version_derive(action="status",version=<父稿>,revision=<修订>)` 的 `review_deliveries`，按文件 ID 实时核验后交付。审阅或发布 PDF 时，先按[文件交付策略](references/file-delivery-policy.md#pdf-审阅与正式发布取件)核对构建、工作修订和正式归档标签。`version` 路线报错时，继续定向查已有审阅回执和 `review_pdf`，按各次实际结果说明交付状态。

## 先选择宿主职责

以当前宿主及可用工具为依据，不以用户提到“Codex”“项目连接”或某个目录来猜。

| 当前宿主 | 主职责 | 入口 |
|---|---|---|
| Codex CLI/App，可实际访问本地工程与命令 | 真实材料整理、PaperWriting 初始化、连接核验及完整交付说明 | [Codex 材料入口](references/codex-bootstrap.md)，完成前必读 [Codex 交付](references/codex-delivery.md) |
| ChatGPT，通过用户选中的 WebCodex MCP 访问工程 | 科研写作、源图编辑、构建、PDF 视觉判断、受控发布 | [Chat 写作入口](references/chat-writing.md) |
| 宿主或工具不可确定 | 先列实际能力，澄清唯一缺失身份；继续可做的只读检查 | [项目契约](references/project-contract.md) |

Codex 负责把实验材料、科研主线、数据与图源、Zotero/BibTeX 和来源索引交入唯一 PaperWriting；Chat 使用这些已核材料完成写作、排版、审阅及获准交付。Chat 每次写作、续写、翻译、润色及返修时，先读取并应用本包的[反防御写作原文](references/anti-defensive-writing.md)，会话接续见 [Chat 写作入口](references/chat-writing.md#普通-chat-的项目与写作风格)。规则随本包交付。Codex 材料交接和纯取件按各自入口执行。

执行前读取当前项目自己的规则和权威材料。使用当前宿主已有的 Python 环境、检索工具和项目索引。用户另装的科研政策、工具链和文献技能可按实际需要调用，本技能包含的核心写作流程可独立使用。ChatGPT 根据实际可见工具操作项目。

## 日常改稿

根据用户指定位置、读者要看懂的问题与所需产物选择工作范围。开始写作时读回工程中的当前稿源和已接受意见，定位本轮可替换的原文或图件。交付实际修改与必要检查。文字和图件的共同审美、各部分的职责及作者反馈沿用[论文表达与编辑验收](references/editorial-review.md)。纯取件按定向入口处理。

## 全文工作流入口

全文创建、重大重构和需要重新设计科学主线的任务，先读取 [全文科研论文工作流](references/full-workflow.md)，再按 REVISION 或 REBUILD 选择入口。该流程使用阶段标签和交接文件，不创建新的运行时状态机。

- REVISION 以已核验版本为基线，保留可追溯历史，先形成修订蓝图再写入。
- REBUILD 以 PaperWriting 原始材料、实验结果、图源和文献证据为输入，不默认继承旧稿结构或贡献。
- 两种模式都先执行 RESEARCH_PREFLIGHT、EVIDENCE_AUDIT 和 STORY_BLUEPRINT，再进入图件、段落、正文、审查、构建和 Drive 交付。
- 轻量局部修订、定向取件和批注返修继续使用现有快速入口，不强制生成完整写作流程文件。

Codex 或可访问工程的本地 AI 准备证据和索引，写作 AI 根据本包的 Brief、章节和编辑规则完成叙事与图件设计。Nature Writing、Nature Figure、literature-research 等用户另装的技能是可选辅助。图件设计先确定科学主张，再核实数据、视觉表达与可编辑源。webPaperWriting 和所选项目连接负责受控编辑、构建、审阅与获准交付。

正式写作前优先建立项目内 WRITING_BRIEF.md。可直接复制 [WRITING_BRIEF 模板](references/writing-brief-template.md)，文献预研按 [RESEARCH_PREFLIGHT](references/research-preflight.md)。已有等价 Brief 时扩展原入口，不创建第二个权威文件。可直接发送的 ChatGPT 提示词见 [ChatGPT prompts](references/chat-prompts.md)。


## 每次新会话

若所选项目 App 附带本技能目录下的 `references/project-connection.md`，首次核对身份时参考其中登记用途，并以实际工具返回的 `project_id` 确认当前论文。该文件由项目 App 单独配置。没有该文件时按既有身份核验接续。

先按本次意图选择入口。**纯定向取件**：已有可信的所选项目绑定和明确版本/修订、语言、角色或图号时，直接按[精确目标取件](references/indexed-targets.md)读取；跳过宽泛 runtime、project inventory、目录遍历及 Drive search，不启动初始化、构建或发布。必要身份核验使用已有定点上下文及本次响应；只有身份缺失、过期或冲突时补最小验证。未启用的旧项目明确说明，不因技能升级假定生产已配置新索引。

初始化、材料整理、建立/变更绑定或需要全面写作上下文的任务，按以下步骤执行：

1. 明确当前项目、任务和既有授权。读项目权威入口及实际状态，不把交接建议视为新增授权。
2. 按实际工具发现 MCP：先用 `paper_runtime_status` 核实所选连接、Runner、发布版本和授权项目，再以真实 `project_id` 调用只读 `paper_capabilities` 与 `paper_project_context`。只使用该项目实际准入的工具和动作；`available` 仅表明配置前提，不替代真实执行验收。旧接口按实际 `runtime_status`、项目发现、`read_files` 接续。Host、Provider、Native 三种身份按 [能力发现](references/capability-discovery.md) 路由，不猜 ID/Schema，不以项目连接显示名或历史工具版本绑定项目。重连或配置变化后重新发现；连接不符只阻断依赖它的写入，本地仍可读取整理材料。
3. 按 [项目契约](references/project-contract.md) 定位 PROJECT、HANDOFF、状态和材料。新写作根默认 `<已核实工程根>/PaperWriting`；已有权威稿库通常原位映射。用户明确要求实体归集到工程下时，执行来源/依赖/哈希核对、历史保留和唯一主稿切换，不用“原位映射”否决已有授权。
4. 读取项目锁定的期刊、文章类型、官方规范、模板和版本哈希。只有选择云规范库的项目才核验 Drive 的 ACTIVE_LIBRARY.json 与 manifest；本地来源可独立接续。公共规则见[双库协作](references/dual-library.md)。
5. 完成已授权实际产物与验证；分别报告事实、构建、视觉、传输和接收状态。失败保留真实错误，不用模拟、文件存在或 exit 0 代替验收。
6. **Codex 初始化与材料整理交付必须向用户说明**：实际 PaperWriting 路径；科研主线、方法、设置与结果的材料简介；引用/Zotero/全文数量和位置；Tunnel/实际项目与权限；Drive 项目名称和路径；Chat 端要求与可直接发送的推荐提示词。按 [交付清单](references/codex-delivery.md) 填实际证据，未知明确列缺口，不能只说“整理好了”或只给文件链接。纯取件只交请求的目标与必要身份，不套用完整初始化清单。

同一会话后续“继续润色/优化图3/重新构建”沿用已确认项目；实际工具仍需宿主授权且入口可见。当前消息不可调用 MCP 时，重新选择同一连接，不声称已经执行。

## 项目写作与交付

先按[文件交付策略](references/file-delivery-policy.md)读取当前连接设置。选用 Drive 的项目复用已核文件 ID，新增展示通过获准的 Publisher 或单文件入口上传，再核对文件身份并给出链接。无 Drive 时可提供本机产物，支持文件接收的 MCP 宿主按用户选择及授权交付最小范围。只有 `drive_links_only` 连接限制为 Drive 链接。构建、上传、客户端收到文件和实际视觉审阅各自记录。

Chat 展示已上传的论文时，按[Drive 稿件展示格式](references/chat-writing.md#drive-稿件展示格式)给出版本状态和指向各份 PDF 的直达链接，标题写清语言、稿件类型与页数。已有对应文件时直接展示。需要生成对照稿时，按[轻量文字对照](references/review-confirmation.md)生成一份红色删除线、蓝色下划线的标记 TeX 并编译审阅 PDF，保留当前稿号。作者确认后按本轮要求保存工作修订，主版本升级和正式发布各自核对指令。需要逐项审批时再选已启用的结构化审阅工具。
每个项目由实验会话交付可核验材料，写作会话按目标范围修订。读取最新材料交接的来源、影响章节与前驱版本。章节稿保存双语正文、共同基线和变更对照。图件保留可编辑源与绘图数据。全稿、章节、单图应明确标注。
 
文字审阅选用 Drive 时只展示本次审阅 PDF，使用当前已配置的单文件渠道并核验身份。用户要求正式发布时，通过本地 Publisher 执行相应归档和发布，分别提供语言和范围正确的链接。用户明确选择无 Drive 的本地交付时，提供本机文件位置；Chat 文件交付按[文件交付策略](references/file-delivery-policy.md)执行。记录实际构建、接收和审阅的状态。完整操作见[章节与材料交接](references/section-workflow.md)及[交付说明](references/build-version-delivery.md)。

## 按需参考

- 本地材料与幂等初始化：[codex-bootstrap](references/codex-bootstrap.md)
- Codex 完成情况与科研材料简介：[codex-delivery](references/codex-delivery.md)
- 可直接填写的 Chat 交接提示词：[chat-handoff-template](references/chat-handoff-template.md)
- 写作、图件、审阅：[chat-writing](references/chat-writing.md)
- 统一工具、项目准入和调用身份：[capability-discovery](references/capability-discovery.md)
- 已知版本、语言和图号的只读取件及增量修订：[indexed-targets](references/indexed-targets.md)
- 身份、路径、状态：[project-contract](references/project-contract.md)
- 必需契约、推荐布局、文件职责与四类索引：[workspace-layout](references/workspace-layout.md)
- 实际 Tunnel 清单、凭据诊断与安全注册：[tunnel-management](references/tunnel-management.md)
- ACTIVE规范入口、双库与版本锁：[dual-library](references/dual-library.md)
- 源文件增量镜像及不可变版本：[drive-sync](references/drive-sync.md)
- 启用受管版本的项目（restore/revise/promote/activate，文献/支持文件依赖候选与增删图表）：[managed-versions](references/managed-versions.md)
- 文献索引、稳定引用键：[zotero](references/zotero.md)
- 事实/工程/视觉/交付门槛：[quality-gates](references/quality-gates.md)

科学数据和冻结历史只读。新项目的实体构建工件默认归入唯一 PaperWriting 的受保护 `builds/` 子树；历史外置布局及获准迁移按 [工程结构](references/workspace-layout.md) 处理。普通编辑不能改 builds/。未启用受管版本的项目持续编辑、不自动派生论文版本；单纯文字对照保留当前稿号；已启用批注确认且用户要求保存修订时再按确认项进版。仅使用旧受管流程时已发布版本整体冻结，按[受管版本](references/managed-versions.md)先 restore 新版本再编辑；`.bib`、支持文件和图表增删也在版本内经依赖候选与结构声明完成，不回到旧候选稿。任何目录权限都以真实服务配置为准，PROJECT 的声明不会自行授予权限。

