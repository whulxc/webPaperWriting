# 受控构建、版本、PDF 审查与 Google Drive 交付

本文中的 Drive 路线适用于已配置且选择该渠道的项目。无 Drive 的项目可继续本地工作，并按[文件交付策略](file-delivery-policy.md)使用实际可用且获准的 MCP 文件接收。`drive_links_only` 只在连接明确返回该设置时生效。

## 已有目标的只读取件

已有 TeX 的文字对照按[轻量审阅](review-confirmation.md)沿原模板生成一份标记 TeX 和审阅 PDF，单语言任务只构建该语言。按需使用指定页预览；正式发布时再核当前修订的构建证据。

用户只要已明确版本/修订、语言或图号时，优先按 [精确目标取件](indexed-targets.md)直读索引及已登记Drive ID，不先盘点runtime/工程或搜索Drive，不为读取而重建、重传或新建正式版本。默认先取 metadata 及已有 Drive 链接；原始 PDF 的内容分片和真实文件按下文附件例外读取；独立论文图和整页PNG预览明确区分。索引未启用、目标缺失或当前PDF已失效时报告真实状态，不默默换版。下面的构建/归档/发布步骤仅用于对应的已授权任务。

## 构建入口

先以 `paper_capabilities` 核项目构建准入，再调用实际可用的 `paper_build_plan`、`paper_tex_build_submit` 和状态/工件工具；Provider 路由使用实际 `plugin_tool` 的 describe→binding→call。旧 `build_start` 只有当前实例仍明确准入且输入身份一致时才可用，不能因统一工具实现而重新开放历史禁用配方。任务中至少明确项目 ID、候选主 TeX、期望源文件版本/哈希、允许的构建 profile 和任务输出根。

引擎与文献工具以当前项目受控配置及真实 `paper_build_plan` 为准：支持配置好的 `pdflatex`、`xelatex`、`lualatex`，按稿件实际引用流程使用 BibTeX 或 Biber，不把 XeLaTeX/BibTeX 固定套到所有项目。Chat 只提交已有 Schema 允许的选项，不添加任意 Shell、TeX hook 或网络执行；缺少依赖或配方时报告具体配置缺口。先取得主稿、输入依赖 SHA 和所需 revision，构建后把 build_id、引擎/文献步骤、日志、PDF 身份和页图关联起来。

受控构建器应检查：目标路径与项目授权、引用的 TeX/include/graphics/bib/cls/sty 依赖、符号链接路径、冻结数据保护、shell escape 与网络策略；构建任务从独立快照读取、输出到任务专属目录，不覆盖正式主稿或旧 PDF。静态依赖分析和编译后的 `.fls` 等记录都可用于追溯；缺少隔离执行时不能假装已完成防护。

新项目的实体工件默认写入唯一PaperWriting受保护的 `builds/` 子树，具体任务/版本布局从真实配置发现，不假设 `artifact_root` 与可见build目录是同一级。只由构建管理机制写入，普通编辑不得覆盖历史；工件不默认进Git或全量源码同步。历史外部布局及获准迁移按[工件存储规则](workspace-layout.md#构建工件的项目内存储)核验，旧清单内的来源绝对路径保持原文，当前读取/恢复路径通过明确映射解析。

长编译采用异步 `submit -> build_id -> status -> artifact`；同一输入与参数重试保持稳定 `request_id`，遇到客户端超时先查原任务。当前接口有 `resume_build_id` 时，恢复按其实际准入执行：确认旧任务已失败/需要恢复、worker已退出、完整源快照与引擎选项一致；新建关联恢复任务，旧终态与日志保留。源已改变时重新规划并提交普通新构建，不冒称同源恢复。配置未启用恢复时报告 `unsupported` 或实际错误；不能借恢复绕过 bNNN 管理。构建失败保留真实错误和日志位置，按任务修复必要缺包、引用或排版。

同一项目同时只运行一个构建任务。`BUILD_BUSY` 时先用 `paper_build_status` 查明阻塞任务：仍在运行就等待其终态；`recovery_required` 且源未变的 TeX/Drive 任务按上文恢复；worker 已失效又不能同源恢复（源稿已改、或图件/PPTX/PDF 任务）时，报告阻塞任务 ID，由 Codex 维护端用本机 `management.paper_build_recovery`（先 `list`/`inspect`，`close` 默认 dry-run，需 `--authorized` 和具体原因；保留全部证据、不删除文件）收口，Chat 不能调用该 CLI。`BUILD_RECORD_CORRUPT` 表示某个历史记录不可读，同样交本机核查，不换 request_id 绕过。

`paper_build_artifact` 的 `metadata` 和 `copy_to_candidate` 用于查工件身份及项目内落盘。`image`、工件 `export`、资源链接和二进制 `chunk` 涉及宿主接收文件，按下文所选交付渠道处理。需要将文件传给 Chat 时，按[文件交付策略](file-delivery-policy.md)选择当前连接允许的动作。`HOST_ROUTE_REQUIRED` 表示动作的调用归属，按本轮用户选择和当前准入处理。

## 版本合同

- 编辑候选可以反复迭代；**发布版本不可变**。每个已发布版本包括源 TeX、BibTeX、原生 PPTX/脚本、图件、构建配置、所用期刊规范快照、PDF、日志、manifest、校验值、变更记录和可恢复的来源。
- 大型原始科研数据不必每版复制，但要保存不可变数据来源标识、依赖和 hash。
- 同一论文工作目录持续编辑，构建 rb/bNNN 和不可变审阅版本分别保留身份；不因构建或发布自动派生新论文工作版本。只有用户明确要求时使用实际准入的 derive 接口，不能覆盖历史。用户只要求局部改动时限制在必要文件与依赖。
- 仅使用旧版受管写作流程且用户已确认进入编辑时，以 CURRENT 和版本状态为准：已发布版本冻结，新一轮修改按[受管版本](managed-versions.md)先 restore 新版本，这是该项目进入编辑的既定前置步骤；构建、promote、Drive 发布与 activate 仍是各自独立的动作。
- 多会话并发依赖 revision/版本检查、Git/worktree 及冲突合并，不允许最后写入者无条件覆盖。

## PDF 自动与视觉审查

最小技术检查：编译 exit code、引用/交叉引用、PDF 页数/页尺寸、字体嵌入、图像质量/裁切、异常留白、表格溢出、图文空间和目标期刊单/双栏规范。

Runner 先执行本机 PDF 检查并按需渲染页面。Chat 需要视觉检查时，从已核 Drive PDF 按页查看，或经当前已授权的展示入口上传本次所需页预览并按其 Drive ID 查看。已存在正确文件时复用其 ID。记录“共 N 页，已检查页码 ...”和具体问题对应的页码、图号及源文件。没有实际看到页面时，记录技术检查结果和视觉检查缺项。

## 默认Drive交付与附件例外

文件向 Chat 传输前，先应用[文件交付策略](file-delivery-policy.md)。该策略按连接的 `chat_file_delivery` 决定附件请求如何处理。本节保留独立安装的兼容路径，`drive_links_only` 连接只采用其允许的链接和本机处理。

已配置 Drive 的项目按同一原生路径执行：所选项目 Runner 读取与编辑项目源码，受控构建器生成 PDF，Runner 的 PDF 工具检查并渲染所需页，原 Publisher 直接读取本次构建产物上传已绑定的 Drive 目录，核对远端文件 ID、父目录、大小与服务端 SHA-256。Chat 读取任务、发布回执、文本和元数据。论文 PDF、独立图件、PPTX 和页预览需要在 Chat 展示或视觉审阅时，使用本项目已核 Drive 直达链接或 Drive 连接器按文件 ID 读取。已有正确对象时复用链接。新增展示只调用当前项目已支持的单文件或原 Publisher 入口，核实际文件 ID、类型、大小、SHA 和父目录；展示动作沿用原稿号，正式归档按作者发布指令执行。

PPTX 原生作业完成后，按实际工件清单使用 `paper_build_artifact(action="copy_to_candidate")` 保存可编辑原件至项目获准的新候选路径，并保留复制回执中的路径、SHA、大小、来源 `build_id` 和工件路径。按该候选路径继续对象检查、后续编辑和导出。交付合成版及单页版时，分别记录文件身份及其共同图件候选。仅生成在 Chat 临时目录的文件，工程交接状态保持待回收，直至实际导入成功。

图件采用 [Chat 写作入口](chat-writing.md) 的本地制作或云端制作路线。本地制作从 `PaperWriting` 可编辑源执行，核落盘 SHA 后导出 PDF/PNG 并同步审阅文件。云端制作允许先上传原始 PPTX 到本项目已绑定的 Drive 目录，作为图源桥接文件，记录 ID、大小、源 SHA 与可用的服务端校验值；随后按该 ID 回收至获准的本地候选路径，核实际字节和 SHA，再完成项目预览与候选登记。工程交接完成以原件回收成功为依据。候选确认和正式论文发布按作者指令执行。

PPTX 桥接和审阅副本使用 `application/vnd.openxmlformats-officedocument.presentationml.presentation`。用户要求 Google Slides 时，另行记录转换稿来源及独立 ID。回执包含制作路线、图源路径与 SHA、桥接或审阅文件 ID、构建输出及其实际核验结果。同一轮合成版与单页版关联同一候选，按需各交其文件身份。


连接返回 `drive_links_only` 时，展示和视觉审阅使用项目 Drive 路径。不要通过项目连接直接返回 `action="image"`、工件 `export`、ResourceLink/resource、预览 widget、二进制 `chunk`/`content`，也不创建聊天下载按钮或附件。文本读取、元数据查询、Runner 本机渲染和检查、项目内 `copy_to_candidate` 继续使用原生工具。`paper_pptx_edit(action="export")` 是本地渲染作业，输出先留在工程，再按已核 Drive ID 展示。

附件请求、缺少原始字节和无 Drive 时的交付选择统一按[文件交付策略](file-delivery-policy.md)处理。兼容模式下执行已明确选择的文件传输时，沿实际 Schema 和宿主批准流程核接收字节与 SHA。

Drive 上传或按 ID 读取失败、类型不受支持、缺少校验值或页面暂不可查看时，记录具体错误、已完成的本地产物和后续所需步骤。继续已授权的修复；`drive_links_only` 模式继续按已核连接设置处理。Publisher 自己执行上传与完整性读回按其原有权限处理。

文字对照只上传本次审阅 PDF，记录原稿与候选输入身份及实际 Drive 文件 ID。沿用原工程依赖，普通构建产生的快照和临时工件由构建器管理。提案登记、论文版本派生和整包 Publisher 均无需作为该操作的前置步骤。

用户要求正式版本交付且项目选用 Drive 时，本地完成构建和不可变归档，Publisher 按已核清单上传。受管归档可能包括源文件与依赖，发布前检查范围。Chat 的成品回复使用[Drive 稿件展示格式](chat-writing.md#drive-稿件展示格式)，逐份列出对应 PDF。只启用版本发布也能工作，源同步是另一项能力。

项目未配置 Drive 时，可继续本机写作、构建、检查并提供实际本机路径。用户明确选择本地交付时沿该选择执行。需要在 Chat 展示文件时，按[文件交付策略](file-delivery-policy.md)处理。兼容模式的章节附件入口见[章节交付](section-workflow.md)。区分已构建、已获批准传输、已收到文件和实际审过的页码。

## Google Drive 发布与核验

- 文字审阅优先使用已配置的单文件展示能力。正式版本交付时，本机发布器（实际配置独立OAuth/同步时）按本轮授权清单将PDF、源文件、图件、页面PNG/索引、manifest和审阅记录上传到项目私有目录；不能因ChatGPT已连接Drive就假设本机有凭据。
- 记录真实 Drive File ID、URL，并由原 Publisher 或 `paper_drive_verify` 核服务端 SHA-256、大小与父目录。服务器 SHA 不可见时如实报告并注明已有回执的来源。Chat 以实际核验结果说明状态，完整下载与聊天附件依上述例外处理。
- 区分阶段：`build_completed` / `upload_completed` / `drive_readback_verified` / `chat_pdf_access_verified` / `visual_review_completed` / `human_approved`。上一状态绝不等同下一状态。另明确 `chat_pdf_full_bytes_received` 和 `chat_pdf_sha256_independently_computed`；只有实际取得完整字节并独立计算才为true。未要求完整下载时写“未执行（本轮不要求）”，不因此判定正常Drive交付失败。
- Publisher的源哈希/远端校验、ChatGPT的元数据核验、ChatGPT逐页视觉观察、ChatGPT完整字节重算分别记录；不能把Publisher结果称为ChatGPT完整字节独立核验。
- 用户在 Drive 下载 PDF 后于外部批注再回传时，按[文件交付策略](file-delivery-policy.md)和实际导入入口接收。重新导入先核版本/hash再修改源稿。

## 非成功路径

没有实际编译 PDF：交付编辑后的章节/TeX 并说明缺少编译。
PDF 已生成但未上传：提供build_id、实际工件路径/metadata和上传错误，保留本地成果；不声称Drive交付，也不自动改为附件导出来绕过失败。
Drive 已上传但 ChatGPT 无法读取页面：提供真实 Drive 链接，报告未完成视觉检查，不虚构查看结果。
