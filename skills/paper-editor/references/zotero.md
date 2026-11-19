# 文献身份与项目引用

检索和稿件证据审查优先复用项目文献索引、合法公开来源和实际已安装的文献工具。本技能维护 `paper_zotero_*` 的项目范围和引用核验流程。Zotero 为可选集成，未配置时使用已有 BibTeX 和来源记录继续写作。使用 Zotero 时核对实际库、版本和授权范围，首次集成先 dry-run。

先查 `paper_capabilities` 的该项目 Zotero 动作范围，再按真实状态查询库与 Collection。另一项目已有持久授权、同一 Windows 桌面或已安装 Skill，均不自动授予当前项目导入、集合管理或 BibTeX 写权限。`not_configured`、`permission_denied`、冲突与网络错误分别保留，不静默切到其他连接。

复用实际可用的 Zotero Local API、已安装插件或 `paper_zotero_*`，先核版本与Schema；不装第二个 MCP、不写 SQLite。Codex 宿主读已安装 Zotero Skill 并先做实际状态检查；WSL 按已验证宿主路径访问，不猜 localhost 可达。本地PaperWriting默认保留索引和必要BibTeX，不为材料归集复制个人书库。全文读取/补取/上传按已有用户授权执行，不因本地采用索引就遗漏已授权的Drive完整全文归档。

从实际 .bib/TeX 引用发现项目 Citation Keys，查项目配置允许的 DOI/Collection。按规范化 DOI 精确去重并比较题名、作者、年份；冲突返回 conflict，不改旧条目。个人 My Library、库标识、Collection Key 以返回值为准。库内 ID、项目 Citation Key、库 Citation Key 三者不同；库 Citation Key 缺失是允许的真实状态。

只读验收依次实际执行 `paper_zotero_manage(action="status")` 或可用的 `paper_zotero_read(action="status")`、项目范围内的条目/Collection 查询、`paper_zotero_verify_citations`，再做 `paper_zotero_sync_bibtex(dry_run=true)`。这些步骤分别验证连接、文献身份、当前引用映射与拟写入差异，不能仅因能力显示 available 就宣布 Zotero 集成通过。按实际 Schema 区分项目引用键、Zotero item_key、原生导出键和持久 Better BibTeX 键；后者未提供则明确 `UNKNOWN`，不从题名推算成已验证的持久键，也不自动替换现有正式键。无 DOI 文献使用项目已授权且可核实的条目身份，不虚构 DOI。

建立 references 下可审查索引（已有索引则复用）：project_citation_key、DOI、Zotero item_key、collection_keys、library_id、library_citation_keys、metadata status、attachment status、合法全文引用/验证状态、source Bib SHA。附件存在不表示已获得完整字节，也不证明句级支持。

计数分别保存当前稿唯一引用键、Bib条目、Zotero匹配/冲突/缺失、去重完整PDF、云端已上传读回数和分母。一个PDF对应多引用键时保留映射，不能靠复制凑数量；一个引用有多版本时明确所选版本。全文缺项/错误身份/预印本版本要列出，不为填齐而改引用或悄悄加入Zotero。Drive全文交付见 [Drive流程](drive-sync.md)。

实际存在时使用 `paper_zotero_read` action=status/search/item/collections/attachments/export_preview。以下仅参数形状示例，项目ID和检索值须替换为实际已核对象：

```json
{"project_id":"verified-project-id","action":"search","query":"verified-doi"}
{"project_id":"verified-project-id","action":"export_preview","citation_key":"existing-citation-key"}
```

导入 `paper_zotero_add_reference` 需 citation_key/doi/collection_name/request_id；默认 dry_run。只在项目单篇明确授权内设 dry_run=false, authorized=true。本地持久 grant 不能扩大项目 DOI/Collection 白名单；别的项目重新明确其范围。请求响应丢失先查询真实 DOI/Collection，不重复写入。

`paper_zotero_sync_bibtex` 用 bib_path/tex_paths/dry_run=true 展示差异；apply 还需服务写配置、authorized=true、最新 numeric read_revision 与 expected_sha256。保留手改和现有键，不自动重写 TeX、不删无关条目。受管版本项目的 bib_path 用 draft 的 `working/deps/<bib逻辑路径>` 副本，写入后按[受管版本](managed-versions.md)捕获依赖候选并 revise。

`paper_zotero_verify_citations` 相同 bib_path/tex_paths，输出 Bibliographic Verification 与 Claim Support Verification。元数据一致不能证明论文支持正文，未核合法原文位置必须 claim_support=unverified；禁止虚构页码/实验结论。

Chat 中取得的本地附件路径只是元数据，不能当作可访问链接。全文按项目索引中的 Drive File ID 与授权文献镜像读取，分别报告元数据可读、实际正文已读、远端校验值以及是否独立取得完整字节；附件存在或哈希记录不能代替原文支持核验。新增/修改 Zotero 条目和 Collection 的实际写入仍需明确授权，已配置只读访问或 BibTeX 同步 dry-run 不产生书库写入授权。
