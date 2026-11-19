# 精确目标取件与增量工作修订

仅对当前项目实际启用并准入的目标索引适用。共享技能更新不启用旧工程，也不增加项目读写、Drive或发布权限。

## 纯取件先直达目标

已有可信的所选项目绑定和明确目标时，直接按版本、修订、语言、角色、图号查询；不先做宽泛runtime、project inventory、目录遍历、Drive search或历史回顾。必要身份验证使用既有定点上下文与本次返回的项目/配置身份。身份缺失、过期或冲突时才补最小验证。用户明确要求目录、索引导入或修复时可按其授权执行有界维护，不把该授权沿用到纯取件。

Host实际存在 `paper_index_get` 时使用其真实Schema：`project_id`、`version`、`role`、`language`，可给明确 `revision`；图件加 `figure_number`。角色为 `manuscript_pdf`、`manuscript_tex`、`figure_pdf`、`figure_source`、`review`、`provenance`；语言为 `en`、`zh`、`shared`。只读目标不产生新任务、版本或Drive写入。

旧客户端未暴露直接callable、但当前连接已启用兼容入口时，可用 `paper_read_files` 读取显式URI，每个item只含 `path`，不带文件系统行号等选项。**元数据一次支持最多8个显式URI**，不必逐图重复发现；缺失目标逐项返回缺项，不扫描目录或搜索补猜。单URI示例：

```json
{"project_id":"当前已核实项目ID","items":[{"path":"paper-index://v1/rev-002/zh/figure_pdf/16"}]}
```

`v1`、`rev-002`只是格式示例，必须替换为已知真实标签。URI顺序为 `paper-index://version/revision/language/role[/figure_number]`。传统数字图号范围为1–999；正文不附图号。启用具名对象的版本还支持在角色后追加 `/key/名称`，按登记的 `target_key` 读取补充图、表、分章正文或具名图源。修订未指定时直接工具省略 `revision`，URI用 `~`；服务先解析同名正式release，否则解析工作HEAD。记录实际 `resolved_version`、`resolved_revision` 与 `resolution_kind`，不以mtime猜最新。缺项结束本次查询，不自动全盘搜索。

## 内容与身份

默认先取紧凑metadata；核 `role`、`language`、`figure_number`、`sha256`、`bytes`、`revision_sha256`，以及实际来源/继承与 `current_for_revision`。`drive_receipt.file_id/url` 是这个确切目标的已登记引用，沿ID读取，不另按名称搜索。历史API审计不代表本次发生上传，也不应挤占Chat目标摘要。

需要源文字内容时，或[文件交付策略](file-delivery-policy.md)允许兼容模式中的本轮附件传输时，`paper_index_get(part="content", offset=..., limit=...)` 每次最多262144字节。兼容URI追加 `/content/offset/limit`，**内容分片每次只读一个目标范围，不能与多URI元数据批次混用**，例如 `paper-index://v1/rev-002/zh/figure_pdf/16/content/0/131072`。base64与metadata也占响应预算，按实际 `max_result_bytes` 留余量；不能把262144字节原文件块假定装入同样大小的JSON。逐块核 `chunk_sha256`，按 `next_offset` 继续，完整接收后才能核总字节和整文件SHA。未完整读取不声称完整字节核验。

独立论文图、整稿PDF和整页预览PNG的角色明确区分。元数据、ResourceLink、Drive上传、完整字节接收、实际页面视觉检查和客户端侧栏显示分别验收。用户只要正文时在Chat中直接给可读正文/公式，沿已有展示规则；纯取件不启动初始化、审稿、构建、同步或发布。

## 增量修订与继承

先按[修订展示与批注确认](review-confirmation.md)处理用户意图。已有两份 TeX 的文字对照直接生成审阅件；保存工作修订或明确授权本地迁入时再使用下面的登记规约。需要逐项审批时可选已启用的结构化审阅。

写动作只在用户授权、索引已启用且实际准入 `paper_index_revise` 时执行。缺少写入口时由Codex维护端按既有管理流程登记，不把只读URI当写入口，不直接改受保护DB、索引节点、冻结manifest或来源指针。索引工作修订不替代项目已有受管版本的restore/revise、正式promote/activate或作者批准。

`paper_index_revise`明确给 `version`、新的 `revision`、`baseline={version,revision}`、`members`、可信当前工作 `expected_head` 和稳定 `request_id`；选择既有图版本时用实际 `figure_selections`。每个新成员给项目内获准 `path`、当前 `sha256`、准确 `role/language`；图件给 `figure_number`，按需给 `figure_version`。使用本次读回身份，不把旧修订或正式别名SHA冒充当前工作HEAD；新工作版本的HEAD预期为 `absent`。

新格式**没有旧的每修订32项总量限制**。每次提交仅计算 `members + figure_selections`，合计最多1024项，且完整控制请求不超过256KiB；未改的选择项继承，不占本次变更名额。不要为满足旧32限制把完整论文拆成伪版本。已启用项目的旧有32项有界清单按点名读取兼容，派生时按需转换，不扫描所有旧版本或重写原哈希。

未改对象与已核Drive ID可以继承，明确实际 `effective_version/effective_revision`、图版本和SHA。文本或选图改变后，对应语言PDF在未提供新准确构建成果时会失效；`current_for_revision=false`不能当当前稿交付。实际重建并登记新PDF及其来源，未改语言按服务的真实继承合同复用。可编辑图源与图PDF的语言、图号、图版本和生成证据一致；不能用旧图源冒充新选图的来源。工作修订、正式发布和Drive展示是独立动作，不自动升级正式版本或复制整个包。

## Drive与失败

已登记目标先复用正确回执ID与角色/语言/SHA。确需新增展示时，只用当前项目已授权的本地Publisher流程与明确父目录，核服务端checksum、大小及父目录；同对象不重复上传，不覆盖历史文件或改分享权限。不要把本地维护端的精简展示能力等同于公共 `paper_publish_to_drive` 已支持任意成员筛选。需要在 Chat 展示或视觉审阅的 PDF、图件、PPTX 与页预览沿已核 Drive ID 读取；附件请求按[文件交付策略](file-delivery-policy.md)处理。Publisher 结果按其真实来源标注。

`INDEX_NOT_CONFIGURED`表示当前工程未启用；`INDEX_TARGET_NOT_FOUND`表示明确键缺失，不能悄悄换版或搜索补猜；`INDEX_TARGET_STALE`要求重建/登记对应PDF；`INDEX_REVISION_CONFLICT`或HEAD冲突要求按实际当前身份重新核对。超时先按原任务/回执/固定ID对账，保留旧终态及部分进度。只有真实已执行的阶段可写为成功。

新技能包保存不等于旧会话已加载新指令；新会话或显式重新读取的实际回证才算加载验证。仅刷新受影响的同一连接，不重建Tunnel或改绑其他项目。
