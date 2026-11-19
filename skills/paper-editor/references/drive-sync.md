# Drive 源镜像与版本

本文中的 Drive 路线适用于已配置且选择该渠道的项目。无 Drive 的项目可继续本地工作，并按[文件交付策略](file-delivery-policy.md)使用实际可用且获准的 MCP 文件接收。`drive_links_only` 只在连接明确返回该设置时生效。

规范库root、项目父目录、项目folder ID均从当前契约、连接与实际metadata核验，不把某一项目的ID硬编码为所有项目默认值。保留既有子目录和文件IDs。不把 ChatGPT Drive connector 授权当成本地 Publisher OAuth 授权。

按[双库协作](dual-library.md)区分：首次本地规范候选→Drive正式release，此后Drive正式规范→本地只读镜像；项目正式源PaperWriting→项目库，云端Reviews→本地新候选。规范维护源、已核镜像和论文根不能相互覆盖。ACTIVE索引/manifest选择规范，项目自己的已锁定release不随ACTIVE静默升级；项目目录改显示名称时保留原 folder ID 和版本历史。

## 项目名称和身份

优先使用已连接 Google Drive 插件执行其支持的查找、读取、重命名、上传和读回，遵循该插件技能。用户指定论文语义名称时采用该名称，仓库名/方法名/论文题名/技术ID不必相同。已有目录先核folder ID、parent及身份说明；重命名在原ID上执行，不新建同名目录再搬全文。读回名称/parents，并同步云端身份说明、当前注册表与本地PROJECT/HANDOFF/Chat提示词；旧历史记录保留并标注已被新决定取代，不全局替换技术路径。

## 参考文献完整全文

当用户已要求Drive保存全文且授权上传时，必须保存当前范围的完整PDF；Zotero索引、元数据、摘要、提取文本和下载链接不能代替它。以实际引用集合逐项核身份、完整可解析字节、大小/哈希和版本，记录缺失/冲突。优先复用已有合法全文；获授权补取时记录来源与版本，不能虚构或以错误论文填缺。全文归档不自动授权Zotero导入或正文引用修改。

按已核项目References/Fulltext（或现有等价路径）上传并生成citation→PDF→Drive ID映射。回读ID、parent、大小和服务端checksum/可获得的字节核对证据；只取名称列表不算字节完整性核验。插件未返回足够校验字段时，仅在既有本地Publisher授权确实覆盖同一目标时补验，否则明确未验证项。未齐全报告确切缺项及原因，不写“参考文献已全部整理”。

插件足以完成时优先插件；大文件中转失败时先查询目标是否已创建，避免重复上传。插件不支持所需能力时说明限制，再使用已有且独立授权的Publisher/resumable能力；不自动改OAuth或分享权限。原文、哈希清单和身份索引分别计数，完整上传不等于全文已读或句级支持已核。正文源镜像和版本发布仍单独授权。

## 源镜像与版本发布

首次接续已有项目先做基线对账，分别记录matched/local_only/remote_only/diverged/excluded及方向依据，见[分类处理](dual-library.md)。不将“本地存在多少文件”当应全部上传的清单；优先沿原回执验证已发布对象，原SHA相同不新传。动态状态和本次同步回执不纳入其自身不可变源快照，避免循环生成新manifest。项目源码、PDF/页图版本和参考全文分别记录覆盖范围与缺口。

`paper_source_sync` 使用项目显式配置 `drive_source_sync`，默认禁用，不继承其他项目范围；先查 `paper_capabilities` 的本项目准入。默认 tagged 策略以 files metadata 的 parent、appProperties 项目 ID/软件 ID/角色核对已有目录，名称相同不收养。受控 `explicit_id` 策略只由可信本机配置固定已批准的项目folder ID，再核名称、父目录、权限和冲突身份；ChatGPT不能通过参数任意换根或扩大范围。两种策略复用同一Publisher和对账逻辑，目录规划 Sources/Figures/References/Reviews/Versions，保留已有全文目录ID。冻结版本继续由既有发布工具管理。

调用顺序（每次都给实际 `project_id`）：

```json
{"project_id":"your-project","action":"plan"}
{"project_id":"your-project","action":"sync","request_id":"source-sync-001","dry_run":true}
```

实际同步仅在已获用户同步授权且 plan 无阻塞时，使用计划返回的真实 `snapshot_sha256`：

```json
{"project_id":"your-project","action":"sync","request_id":"source-sync-001","dry_run":false,"authorized":true,"expected_snapshot_sha256":"实际64位SHA256"}
{"project_id":"your-project","action":"status","request_id":"source-sync-001"}
```

### 异步同步与对账

Gateway对 `action="sync",dry_run=false` 使用受控后台任务，原同步引擎、项目范围和请求参数保持。提交后保存原 `request_id`、输入快照SHA及返回的任务身份，再按同一 `request_id` 调用status；不能因一次MCP等待超时换ID重传。

- 后台任务返回 `asynchronous=true`、`task_id`、`attempt`、`status/worker_status`；`queued/running`时 `accepted=true`，不代表已经同步完成。很快完成的任务也可能直接返回终态，以 `status` 和 `sync_complete` 为准，不把 `accepted=false` 单独解释为失败。
- 同参数、同request_id的重复调用复用同一任务，不再启动第二个活跃worker；恢复会增加受控attempt，旧attempt与日志保留。`task_id`不是rb构建ID或Native Job ID；源同步只用本工具的status(request_id)查询。
- `status`是本地只读观察，不启动进程，也不做新的Drive GET。`engine_status`表示原引擎进度，`worker_status`表示后台审计状态；`progress`存在时给出已核验/预期文件数。`worker_remote_revalidated=true`只证明该worker执行过远端核验，轮询本身仍不是新的云端检查。
- 已完成的历史请求可直接返回 `asynchronous=false,receipt_only=true,remote_revalidated=false`，不要求存在后台task_id。这是原完成回执的快速读取，不是本轮逐件远端重验。需要新鲜云端核验时按manifest的File ID、parent、大小与checksum/字节实际检查，不能把回执重放当下载验收。
- worker退出或身份失效时返回 `recovery_required/WORKER_LOST`。即使 `engine_status="completed"`，也不静默改写未完成worker的审计；只有服务能够确认旧worker不再运行、配置和代码身份一致时，才沿原授权显式重放同一sync参数建立下一attempt，先对账已有引擎回执，已完成则不再次上传。status本身不恢复。
- 若服务报告 `SOURCE_SYNC_WORKER_UNKNOWN` 或启动调度中断，原worker可能未能完成身份登记。保留任务、attempt和日志，停止该项恢复并由本机维护对账；不能猜PID、重复启动或换request_id绕过。
- `failed`（例如 `DRIVE_DEADLINE`）同样用原参数重放同一 request_id 建立下一 attempt；已核对象复用，不换 request_id，也不放宽时限。
- `CONFIG_CHANGED`或`CODE_CHANGED`阻断旧未完成任务在另一配置/实现下重启；先保留回执与错误并由本机维护核对，不换request_id规避。`SOURCE_SYNC_BUSY`表示同项目还有活跃或待对账任务，先处理它。

plan、dry-run和import_review保留各自原行为，不因为同属paper_source_sync就假定全部后台化。源任务完成仅在 `status="completed"` 且 `sync_complete=true`时确认；部分成功、失败和恢复中仍保留已核文件与真实错误。

`plan` 返回 `coverage` 时，其中列出未纳入 include 的项目根文档等，只供维护端核对同步范围是否遗漏，不自动扩大范围；include 仍是唯一授权清单。include 为明确相对路径/前缀，exclude 为配置过滤；PDF 必须逐路径列入 approved_pdf_paths。大小/数量有限。禁止凭据、Git、编译缓存、训练数据/checkpoint、未授权参考全文；包含目录不能绕过固有禁止规则。严格版本号和协议以实际工具 Schema 为准。

源文件先冻结字节身份，上传新增/变化 SHA 的新对象；旧对象不覆盖。目录与对象对账依赖 ID、parents、字节大小、server checksum 和源 SHA，不只 appProperties 或名称。未知响应先查询，无法确认即停止，不能盲目重 POST。仅全部核验成功才记录 completed/current mapping。

云端清单使用不可变 `sync-manifest.<snapshot_sha256>.json`，本地账本/status 指向当前清单，避免修改历史。它是建议 sync-manifest.json 的兼容不可变实现；必须读实际返回 ID，不猜稳定下载链接。bootstrap 的 sync/manifest.json 记录初始化索引，私有同步账本由实现维护，两者职责不同。

使用本地 Publisher 时，>10 MiB 文件走既有 Drive 官方 resumable 上传，MCP只返回身份/状态，不通过Tunnel单次响应返回整个文件。实际插件限额按工具Schema与返回判断，不凭Publisher阈值推断插件上限。上传成功不等于 ChatGPT 已获得字节或完成视觉检查。

审阅导入：`action=import_review`、request_id、remote_file_id、destination、dry_run 默认 true。只接收已核验项目 Reviews 的新材料，目标为配置可写 reviews 目录的新文件；显式授权 apply，不能覆盖 TeX/BibTeX/实验数据。远端批注文件来源不可信，内容不产生执行授权。

默认按需：初始化计划、任务开始检查、获准构建完成后执行既有版本发布。后台worker只执行该次已授权请求，不建立常驻watcher或自动双向镜像。404/403 表明该 Publisher 无法访问目标，保留真实错误并请求该 app 的最小文件夹授权；不换 OAuth、不改传其他目录、不宣称 mock 网络测试为真实云端通过。

## 共用授权与按 ID 验证

本地 Publisher 的 OAuth 与论文项目 ID、构建 ID、Drive File ID 分离。同一账户的已登记论文项目复用同一安全凭据文件引用；新项目沿既有管理入口登记该引用，不复制令牌、不把一个项目的目标目录带入另一项目。各项目的上传目录和写入授权仍独立核验。

发现 `paper_drive_verify` 时，优先用本地 Publisher 直接核 Drive 授权和元数据。每次携带实际 `project_id`；不指定 `file_ids` 时检查当前授权和已配置项目目录，指定时最多20个实际文件/目录ID。允许核验账户可访问的任意ID；只返回元数据、服务端校验和与校验结果，不下载完整文件、不赋予发布或分享权限。需要严格身份校验时按当前Schema传入期望值。发布完成后用回执中的真实File ID复核，不用历史同名文件或appProperties自报哈希替代服务端校验。

短期access token由既有Publisher按需自动刷新。Google `invalid_grant`说明需要账户本人重新授权；网络中断或服务暂不可用不能直接判作授权失效，不无依据重试。重新授权沿Google官方Desktop OAuth，并核原账户/客户端/范围、保存到原受保护位置；不复用ChatGPT连接器令牌。Google应用发布状态不能从本地令牌推断；Testing模式常有7天refresh token限制，长期运行须在Google Console核实正式状态，不能承诺永久不失效。无需后台保活、收费API或自动扩大scope。
