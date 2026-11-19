# 受管论文版本：restore → 编辑 → 冻结 → 构建 → 发布 → 激活

适用条件：`paper_runtime_status` 返回的授权项目带非空 `version_management` 与 `managed_version_write_policy`，且 `paper_capabilities` 的 `paper_version_derive.actions` 列出所需动作（`stage`/`dependency` 由 Paper Studio 0.6.0 起提供，旧实例没有时按下文“旧实例”处理）。未启用的项目沿用原 manuscript/workdir 持续编辑，本文件不适用。

“展示修订版”先按[轻量文字对照](review-confirmation.md)从指名的两份已有 TeX 生成审阅稿，沿用当前论文版本号。下述 restore 用于用户已授权保存修改的受管编辑；需要逐项审批时可选已启用的审阅提案。

## 先确定当前稿

1. `paper_version_derive(action="status")` 读 `current`：`active_working_version`、`latest_published_version`、`author_approved_version`、`activation`，记下 `current_sha256`。
2. `paper_version_derive(action="status", version=<active>)` 读 `status`（`draft`/`published`）、`current_revision`、`selected_figures`、`selected_dependencies` 与 `version_sha256`。
3. `status="published"` 表示整版冻结，不能编辑、`revise` 或再次 `promote`；新一轮修改先 restore 为新版本（用户已授权修改时这是必要前置写动作，先 dry-run）。
4. draft 版本只有三类文件可经 `paper_edit_files` 修改：`versions/<v>/working/main_en.tex`、`working/main_zh.tex`，以及 `working/deps/<逻辑路径>` 下的支持文件（`.bib .bst .tex .sty .cls .cfg .def .clo .fd .ldf .csv .dat .txt`）。`VERSION.json`、`revisions/`、`figures/`、`dependencies/`、`release/` 由工具管理。旧的非受管候选稿不是接续入口；以 CURRENT 与项目 HANDOFF/STATE 为准，不维护两套主稿。

## 写动作共同规则

- 每个写动作都要稳定 `request_id`；默认 `dry_run=true`，核对返回后用同一组参数改为 `dry_run=false`。
- `expected_current_sha256` 用最新 status 的 `current_sha256`；`expected_version_sha256` 用该版本 `version_sha256`，restore 新版本时为 `"absent"`。返回 `REVISION_CONFLICT` 时重新 status/读取后再做，不沿用旧值。
- 同 `request_id`、同参数重放返回原结果（`idempotent_replay=true`）；同 `request_id` 改参数返回 `IDEMPOTENCY_CONFLICT`。失败且未提交的写动作留下待对账记录：原参数可以重试；修正参数后必须换新的 `request_id`。

## 标准一轮

| 步骤 | 调用 | 关键参数与检查 |
|---|---|---|
| 1 restore | `paper_version_derive(action="restore")` | `version`=新标签；`source_version`=已发布版本；`source_builds`={en,zh} 为该归档内的两次 TeX 构建；`expected_source_manifest_sha256`=该归档 manifest SHA（`paper_version_list` 的 `manifest_sha256`，或 `current.activation.version_manifest_sha256`，不猜）；`reason`。结果为 draft `rev-001`，并返回沿用的 `structure` |
| 2 编辑主稿 | `paper_read_files` → `paper_edit_files` | 读 working 主稿取 numeric `read_revision` 后精确编辑并读回；SHA256 只作身份 |
| 3 支持文件（可选） | `paper_version_derive(action="stage")` → 编辑 → `action="dependency"` | 见下节；每个改动冻结为 `dep-NNN` 候选 |
| 4 图件（可选） | `paper_version_derive(action="figure")` | `figure_id`=figNN，`candidate_id`=vNNN，`output_path`=该图位的逻辑 PDF 路径；`source_paths`=[{path,sha256,role}]，恰一个 `role="output"` 的 PDF |
| 5 revise | `paper_version_derive(action="revise")` | `expected_sources`={en,zh} 为两份 working 主稿当前 SHA；按需给 `figure_selections`、`dependency_selections`、`structure`+`reason`。先 dry-run 看 `input_changes`（相对 rev-001 的 added/removed/modified）。冻结为新的 `rev-NNN` |
| 6 规划与构建 | `paper_build_plan` → `paper_tex_build_submit` → `paper_build_status` | `tex_path="versions/<v>/revisions/<rev>/main_en.tex"`（中文同理）；严格沿用该 revision 的计划所返回的 `engine`、`bibliography_engine`、`use_latexmk`、`expected_revision` 与 `input_sha256`，按当前 Schema 将引擎映射到 `build_profile`；Schema 支持 `expected_input_sha256` 时传入计划的 `input_sha256`，并使用新 `request_id`，不得自行换配方。旧 revision 缺少新增配方身份字段时保持该 revision 原有默认，不因当前项目设置变化改写历史；中英各一次，轮询到 `completed` |
| 7 逐页审阅 | 项目 Drive 已核 PDF 或页预览 ID | 先在 Runner 构建并检查。Chat 从已核 Drive PDF 查看所需页，或由当前已配置的展示入口上传必要页预览后按其 ID 查看；记录实际看过的页面和审阅结论。文件尚不可查看时报告具体缺口 |
| 8 promote | `paper_version_derive(action="promote")` | `revision`、`build_ids`={en,zh}、`visual_review`={path,sha256,pages_seen:{en:[1..N],zh:[1..M]}}、`changelog`；构建必须属于该 revision 与语言，页码必须完整 |
| 9 发布 | `paper_publish_to_drive(version)` | 轮询 `paper_build_status` 到 `completed` 且 `upload_complete=true` |
| 10 activate | `paper_version_derive(action="activate")` | `drive_build_id`=上一步任务；CURRENT 指向新版本；`author_approved_version` 不由工具代填 |

回到更早版本的内容时用 `action="rollback"`：参数与 restore 相同，另给 `rollback_from`=被回退的版本；结果同样是新的 draft，不改动任何已发布版本。`select` 只把 `current_revision` 切到该版本已有的某个 revision。

构建完成、页图已看、归档冻结、Drive 上传、CURRENT 激活和作者批准是六个独立状态，按实际返回分别报告。

## 文献与支持文件（依赖候选）

- 修改已有文件（如 `.bib`、`preamble.tex`）：`action="stage"` + `path`=逻辑路径（与 `paper_build_plan` 返回的 `files` 路径一致，如 `support/references.bib`），工具把冻结字节复制到 `working/deps/<path>` 并返回其 `working_path` 与 SHA；已存在且相同返回 `already_staged=true`，已被改过返回 `REVISION_CONFLICT`（直接读改即可）。
- 新增文件（如 `\input{sections/appendix}`）：用 `paper_edit_files` 在 `working/deps/sections/appendix.tex` 直接创建，主稿按逻辑路径引用。
- 冻结：`action="dependency"` + `path` + `candidate_id`=dep-NNN（版本内唯一）+ `source_sha256`（working 文件当前 SHA；`source_path` 缺省即该 working 文件，也可指向项目内其他文件，如历史归档成员）+ `reason`。候选不可变，记录 `base`（modify/add）与来源；与当前冻结字节相同会被拒绝（`unchanged`）。
- 选择：revise 的 `dependency_selections={逻辑路径: "dep-NNN" | "base" | "absent"}`；`base` 回到 rev-001 的身份，`absent` 移除该输入。未列出的路径沿用上一 revision。图件 PDF/PNG 不走依赖候选，入口主稿也不能作为依赖。
- 新增引用键后必须实际构建，并在 `.bbl`/PDF 中核对；引用键和句级支持仍按科学证据规则核验，工具只保证字节与身份。

## 增删或替换图表（结构变更）

- 替换已有图：对原图位 `figure` 新候选，`output_path` 不变，revise 时在 `figure_selections` 中选它。
- 新增图：对新的 figNN 捕获 `figure` 候选，`output_path` 为一个尚未作为输入的新 PDF 路径；两份主稿加入对应 figure 环境与 `\includegraphics`。
- 删除图：主稿删去该图，revise 时 `figure_selections` 不再列出该图位，其输出随之移出输入。
- 新增/删除表：只改主稿中的 table 环境数量。
- 图位或数量变化的那次 revise 必须给出 `structure={figures:N, tables:M}`（主稿 `figure`/`figure*`、`table`/`table*` 环境数，`\input` 文件里的环境不计入）与 `reason`；工具记录 `structure_change`，此后的 revise、构建、promote 和由该版本 restore 的后继版本沿用新结构。可信配置里的数量只是基线。
- revise 会先核对两份主稿的环境数与 `structure`、`\includegraphics` 与所选图位一一对应且中英顺序一致；不符返回 `MANUSCRIPT_STRUCTURE_CONFLICT`（带 `language`、`actual`、`expected`）或 `FIGURE_IDENTITY_CONFLICT`，不会冻结半成品。

## 本地激活

已冻结的双语版本在本地激活时显式设置 `delivery_mode="local"`，绑定 `expected_source_manifest_sha256` 与当前状态；该动作只更新本地工作指针。沿用 Drive 策略的项目继续提供已完成的发布回执。

## 比较、发布与验收

- `paper_version_compare` 的 `changes` 列出构建快照中的实际字节差异（含 `.bib`/支持文件 diff）；两侧为受管归档时另有 `managed`：结构、图位与依赖选择的变化。
- promote 后归档含 `dependencies/`、新图位候选与 `release/manifest.json` 中的 `structure`、`dependencies`、`input_changes`；Drive 发布一并上传这些受管记录。
- `paper_version_list` 中某个归档校验失败时单列在 `integrity_failures`，其余照常返回；失败的归档不要用于 restore/compare，报告给 Codex 维护端。

## 其他边界

- 引擎、搜索路径与兼容 hook 来自可信配置，Chat 参数不能改变。
- `checkout` 只登记只读比较基线，不创建可编辑版本。
- 旧实例（`actions` 中没有 `stage`/`dependency`，或 revise 不接受 `structure`）：版本内只能改两份主稿，`.bib`/新依赖/图表数量变更交付正文与具体缺口，由 Codex 维护端处理；不用旧候选稿、复制目录或手工改受管文件绕过。

## 常见返回与处置

| 返回 | 含义与下一步 |
|---|---|
| `VERSION_FROZEN`，或 `WRITE_DENIED`（Published versions are frozen） | 目标版本已发布；restore 新版本后编辑 |
| `WRITE_DENIED`（其他） | 路径不在 draft 的 working 主稿或 `working/deps/<支持文件>` 内 |
| `REVISION_CONFLICT` | 期望 SHA 已过期或 working 副本已改；重新 status/读取后重试 |
| `IDEMPOTENCY_CONFLICT` | 同一 request_id 已绑定其他参数；恢复原参数或换新 request_id |
| `VERSION_EXISTS` | 候选 ID 已被另一请求占用；换新的 dep-NNN/vNNN |
| `DEPENDENCY_UNRESOLVED` | 主稿引用的文件不在该 revision 输入中；按上节加入依赖候选后再 revise |
| `DEPENDENCY_CONFLICT` | 候选的路径与选择不符，或把入口主稿/图件当作依赖 |
| `FIGURE_IDENTITY_CONFLICT` | 图位与 `\includegraphics` 不一一对应、新图路径已被占用，或增删图位未声明 `structure` |
| `MANUSCRIPT_STRUCTURE_CONFLICT` | 主稿环境数与声明的结构不符；按详情修正主稿或声明 |
| `BUILD_IDENTITY_CONFLICT` | promote 的构建不属于该 revision/语言；对该 revision 重新规划并构建 |
| `VISUAL_REVIEW_REQUIRED` | 审阅记录或页码不完整；补齐实际查看的全部页 |
| `DRIVE_INCOMPLETE`、`DRIVE_IDENTITY_CONFLICT` | activate 需要该归档的 completed Drive 任务且全部对象服务端 SHA 已核 |
| `BUILD_BUSY` | 先用 `paper_build_status` 查明活动或 `recovery_required` 任务，见[构建入口](build-version-delivery.md#构建入口) |
