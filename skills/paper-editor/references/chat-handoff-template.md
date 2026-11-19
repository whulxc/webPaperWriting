# 给 Chat 的推荐提示词模板

Codex 按实际任务填写并保存 `CHAT_POLISH_PROMPT.md`（已有等价入口则复用）。交付前替换所有尖括号占位，未知信息写明确缺口；无编辑授权时任务限定为接续核对，不由模板升级为全文改稿。长篇方法/结果放材料入口，提示词保留必要不变量。不得复制任何凭据。

读取路线按工程实际职责填写：PROJECT身份/路径 → STATE动态状态 → README科研简介 → HANDOFF接续步骤 → 四类索引目录元数据，再按本轮任务读相应原始材料。文件来源清单不能代替主张证据，构建输入清单不能代替图件索引或编译验收。目录元数据只是导航，不创造新的科学事实。

```text
请使用「web论文写作 / paper-editor」技能接续〈论文语义名称〉。
本轮任务：〈用户实际授权的接续/润色/图件/构建/审阅任务〉，完成实际可验收产物。
在普通 ChatGPT 写作聊天中选中下面唯一目标 App/Tunnel。首次核真实 project_id，后续沿用同一绑定，用户无需重复 @ 或项目 ID。每次写作、续写、翻译、润色和返修前，读取当前 web论文写作包内 references/anti-defensive-writing.md，并按该原文处理本轮文字。纯取件按定向入口执行。项目工具缺失时，说明具体缺口并给同一连接恢复步骤。

连接与身份：选择〈本篇连接名/alias〉，先发现实际工具。有paper_runtime_status时先调用，再仅对返回的授权项目调用paper_project_context；旧接口使用实际runtime_status、项目发现和read_files。预期Runner〈已核值或未知〉，本地最近核得Native ID〈值或未登记〉；仍以本轮发现为准，不猜ID或使用短别名代替完整身份。
工程根〈绝对路径〉；唯一PaperWriting根〈绝对路径〉。本地契约ID〈值〉和Native ID分开。发现根/权限不符时停止依赖该连接的写入，报告具体缺口；不换绑Tunnel、不借另一篇工程、不假装能运行WSL命令。
Drive项目〈语义名称〉，路径〈完整逻辑路径〉，已核folder ID/链接〈值〉。技术路径与显示名称不同，旧名称只作追溯。

先实际读取：〈PROJECT/HANDOFF/状态/路径映射〉；完整读当前〈版本〉的〈双语或单语源稿、README、修改记录、静态检查、构建交接〉，长文件分段。再按〈材料/图源/文献索引〉读取本任务所需冻结证据、作者批注/响应矩阵和未完成图件任务。最新源稿〈路径及接受状态〉；历史PDF〈版本/路径〉，不能替代新稿PDF。
共享规范先核已知根Folder ID下的ACTIVE_LIBRARY.json及manifest SHA，再从项目已锁定release按相对路径/File ID读取本任务路线和Markdown原件。首次报告实际library_version/release_id、项目规范锁、已读文件/章节和未核项；不按同名文件、旧固定链接或ZIP存在补造读取。ACTIVE有新版不自动升级本项目，规范版本与论文工作/构建版本分开。Drive不可读时说明影响，继续可由已锁定授权材料支持的工作。

允许修改〈实际白名单与本轮任务交集〉；使用工具要求的最新read_revision/SHA并读回，保留人工/并发修改。保持〈标题、冻结方法、样本与统计单位、划分、seed含义、指标/不确定性、硬件口径等已核事实〉。不新增实验、调参、采集、测速、Bootstrap或虚构引用/数据/概率/置信区间，不修改冻结历史或基础设施。
具体工作优先级：〈与用户任务相符的段落/问题/批注〉。已有清楚内容不为制造差异重写。文字处理、图件源码调整、重新渲染和视觉验收分别报告。
文献入口〈本地Zotero/来源索引、Bib及Drive Fulltext路径〉；〈真实引用、匹配、完整PDF/上传数量与缺项〉。完整全文存在不等于已经读过或支持具体主张；需主张核验时实际读取对应页/节。
图件以当前TeX的includegraphics、图号映射、原生图源、实际数据和input_manifest为准；不只改导出的PNG。旧绝对路径/生成器护栏〈适配状态〉，未确认前不执行。
构建〈本轮是否授权、实际服务能力和新输入登记状态〉。获准且可用时按当前输入SHA与独立输出使用项目指定引擎；旧配方不匹配则报告具体注册缺口，不构建旧输入冒充新稿。不自行派生新论文版本或上传发布。

完成后实际核对〈双语/图表/公式/labels/ref/cite/文件依赖等任务所需检查〉，给出可打开的修改源/图源/获准PDF、修改说明和未决项。默认由所选项目 Runner 完成读取、编辑、构建、渲染和本机检查。论文 PDF、图件、PPTX 及页预览需要 Chat 展示或视觉审阅时，经项目实际可用的 Publisher/单文件入口上传并核身份，再给 Drive 直达链接或由 Drive 连接器按 ID 查看。文件传给 Chat 前按[文件交付策略](file-delivery-policy.md)读取连接设置。分别报告静态检查、编译、视觉实查页、云端上传读回及接收状态，未执行不能标通过。
```

当前MCP只有旧接口时，推荐提示词必须明确旧接口路线；只有只读权限时不能写“已经可编辑”。当前根已注册但Chat尚未发现时写“本地已核，Chat仍须实时核对”。若没有明确下一步编辑任务，改为“先完成接续核对并列具体建议，不擅自改稿”。

## 全文写作交接字段

完整任务在当前项目的真实交接文件中增加以下字段，或引用已有等价字段：

- task_mode: REVISION 或 REBUILD
- writing_brief_path:
- baseline_version_or_independent_identity:
- research_preflight_path:
- reference_papers_reading_scope:
- evidence_audit_path:
- figure_storyboard_path:
- paragraph_sentence_plan_path:
- chat_skills_required: web论文写作（含包内反防御规则）
- codex_skills_used: 按实际填写已使用的本地辅助技能或工具，未使用可留空
- selected_project_connection:
- actual_project_id:
- allowed_write_scope:
- build_and_drive_delivery:
- author_decisions_required:
- unresolved_blockers:

Chat 端先读取这些字段和 WRITING_BRIEF.md 的定向引用。字段缺失时说明缺口，不用全量扫描补猜，不把 Codex 本地模拟写成 ChatGPT 端已验收。

