# 双语章节与材料增量示例

本例创建全新的合成科研项目。两个实验批次记录准确率0.81和0.84，共用100个合成留出观测；用于检验流程的数值不构成科研结论。RGB场由固定种子生成，让PDF达到需要多次分片读取的大小。示例Bib条目显式标注为synthetic fixture。

在安装了Linux/WSL TeX Live和Paper Studio运行依赖的环境中执行，下方两个绝对路径由使用者填写。输出目录必须尚不存在。

```bash
PYTHONPATH=paper-studio:. python examples/bilingual_sections/run.py \
  --output-root /absolute/new/example-run \
  --texlive-root /absolute/texlive
```

先在 Python 环境中安装本仓库，再按 [平台安装说明](../../docs/INSTALL.md)准备隔离构建依赖。示例无需已有论文、Drive、Zotero、Native服务或Tunnel。它复用正常材料交接、可信模板、依赖规划、异步构建、隔离XeLaTeX/BibTeX、版本归档和工件分片读取代码。

输出保存在指定目录。

- `project/results/` 保存两轮合成实验摘要。
- `project/PaperWriting/materials/handoffs/` 保存可追溯材料交接。
- `project/PaperWriting/chapters/methods/r01` 和 `r02` 保存同范围的中英源稿与来源清单。
- `downloads/` 保存经分片重组、完整大小和SHA256验证的四份PDF。
- `revision-comparison.html` 显示两种语言的原版与修订行级对照，适合局部文字返修检查。页面版式和图形变化需要结合各版PDF页图判断。
- `report.json` 保存真实build ID、PDF身份、材料版本、归档身份、分片数和验收覆盖。
- `instance.json` 是示例专用配置，初始没有凭据或Drive目录。

本地接收端完成不表示ChatGPT网页已经生成附件或手机已经打开文件。当前Host需要独立执行 `paper_section_delivery` 并实际点击下载，才能建立该端的接收证据。

章节工具通过项目的 `chapter_workflow` 绑定两个SHA256固定模板。每个模板只含一个 `@@SECTION_BODY@@`。`paper_section(action="stage")` 同时提交两种语言、共同scope、材料manifest路径和SHA256，以及上一章节manifest身份。新内容进入新revision，普通文件编辑不能改写这些章节归档。重试相同revision复用完全相同内容，旧baseline、跨项目材料和改动模板会被拒绝。

`paper_section(action="compare")` 提交 `section_id`、`left_revision` 和 `right_revision`，返回安全转义的HTML对照、两种语言的源码diff和两个版本身份。材料变化通过 `material_changed` 显示，科学等价性由作者审查。

受管全文版本可显式选择 `paper_version_derive(action="activate", delivery_mode="local")`，并提交已核归档的 `expected_source_manifest_sha256`。本地当前稿指针随后记录该归档，已有的云发布指针保留原值。省略 `delivery_mode` 时继续走既有Drive回执核验。
