# 项目契约 paper-editor.project.v1

新工程默认实体 `<已核实工程根>/PaperWriting/`，已有权威项目通常原位映射。用户明确要求实体归集时按已授权范围保留来源、记录哈希和依赖并指定唯一后续写作根；不要维护两套可编辑权威稿。仅创建目录或清单不等于材料已归集。下方是推荐布局，必需的是信息职责与可解析的逻辑映射，不强制重排历史物理目录。细则见 [工程结构与四类索引](workspace-layout.md)。

```text
PROJECT.yaml / HANDOFF.md / PROJECT_STATE.json
materials/INDEX_CATALOG.json              # 四类索引的角色/字段/责任入口，可映射等价文件
materials/{file_inventory,claim_evidence,build_inputs,figure_index}.*
materials/{methods,experiments,figures}/
manuscript/  references/  figures/  reviews/  builds/
sync/manifest.json
```

PROJECT 必须表达（未知值用 null，不能猜）：
`project_name`, `project_slug`, `project_id`, `original_project_root`,
`paper_writing_root`, `tunnel_id`, `tunnel_alias`, `gateway_profile`, `runner`,
`target_venue`, `current_manuscript_version`, `writable_scopes`, `readonly_scopes`,
`zotero_library_id`, `zotero_collection_key`, `drive_project_folder_id`,
`drive_knowledge_root_id`, `skill_contract_version`。
同时记录 `native_project_id`（与本地契约ID分开）、`drive_project_folder_name`、`drive_project_path`、实际服务读写范围及 `binding_verification`。这些是向后兼容的契约补充，旧schema通过映射表达，不凭空宣称迁移成功。Drive语义名称由用户或实际论文身份确定，不默认沿用仓库名；更名保持技术ID和已核folder ID，除非用户另有明确要求。
密钥只存安全存储引用；不要将本地 credential 文件内容或个人库数据目录写入交接。

Paper Studio 发布版本、源码清单 SHA、Native 版本与各实例配置身份由管理登记维护，项目引用实际返回的身份，不把某个项目连接名称与固定版本/目录绑定。Paper Studio `project_id` 与 Native `project_id` 分别记录；工具级准入通过 `paper_capabilities` 查询。共享实现、同一运行密钥引用或同一Runner均不合并项目路径、写权限、Zotero白名单或Drive目录授权。

公共规范采用单独的版本锁：实际library_version、release_id、manifest_file_id、manifest_sha256、规范folder ID及采用的入口/规则。项目工作版本、构建记录和规范锁不得混用；ACTIVE切换只改变新默认阅读入口，不能静默更新已有项目。契约受保护时通过已授权映射或审阅记录表达候选采用，并明确未正式换锁。规范维护源/云端正式版/本地只读镜像/项目正式源的角色和覆盖关系见[双库协作](dual-library.md)。

新项目实体工件默认在唯一PaperWriting的受保护 `builds/` 子树，PROJECT逻辑路径同时标明实际工件根与任务/版本子目录语义，服务的 `artifact_root` 和Native工件项目仍须真实配置。历史外置布局、已授权迁移及不可变来源路径按[工件存储规则](workspace-layout.md#构建工件的项目内存储)核对；只改PROJECT声明不能证明历史工件已经迁入或构建访问已经切换。

PROJECT声明身份、路径和scope，动态状态以PROJECT_STATE为准，使用status_source或等价路径引用。AGENTS只保留稳定规则；README维护有来源的材料简介；HANDOFF给接续步骤并引用当前状态。旧兼容字段需注明别名/快照语义，不在多个文件手写互相冲突的“当前状态”。

已有 PROJECT/HANDOFF/STATE 受保护时保持原字节，通过 `sync/paper-editor-map.json` 的路径映射和缺口记录表达新契约。对未授权写入的旧项目只生成计划，不能为补契约放宽根权限。若注册只允许 v0.01，可将新增交接映射放在该版本获准 reviews 路径；由维护端明确注册状态入口。

PROJECT_STATE 复用 `paper-studio.research-state.v1`：schema/project_id/workdir/current_task/accepted_fact_sources/frozen_result_sources/completed_builds/issues/deliverables/last_acceptance。引用含路径与 SHA256；事实及接受状态由真实证据确认，不由初始化自动晋级。已有等价事实源只建路径映射；初始化清单与云同步账本不是第二套科研事实库。

文件来源清单、主张证据表、当前构建输入清单、当前图件索引四种角色必须分开，复用原文件并通过目录元数据说明字段和覆盖范围。旧evidence_registry.csv若只有source/destination/SHA/bytes，应登记为file_inventory兼容路径，不宣称是claim_evidence。每条主张索引至少包含来源文件、SHA256、代码/数据版本或协议引用、定位selector、统计单位/分母、验证状态及稿件位置；未有版本信息列缺口。代码存在只能记implemented/inventory；计划、synthetic、unverified分开。部分关键主张索引不等于全部句子验证。

普通编辑：材料、PROJECT/HANDOFF、冻结 builds/ 只读；当前稿、图源、审阅记录按注册白名单可写。服务可有更严格边界。路径必须项目相对，拒绝逃逸、symlink、凭据路径。更新现有文件使用最新 numeric read_revision；SHA256 用于身份与构建 fence，不替代 native revision。

ChatGPT 读文件每批最多 8 项，输出上限 262144 bytes，长文件分段。读取失败应保留错误，缩小批次并重读；不能根据历史聊天补造当前内容。
