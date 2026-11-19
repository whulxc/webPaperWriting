# 双语章节与材料更新

本文中的 Drive 路线适用于已配置且选择该渠道的项目。无 Drive 的项目可继续本地工作，并按[文件交付策略](file-delivery-policy.md)使用实际可用且获准的 MCP 文件接收。`drive_links_only` 只在连接明确返回该设置时生效。

Codex 先运行初始化器，然后实读科研材料。新实验通过 `python -m paper_studio.bootstrap.handoff` 交接。填写项目 ID、前驱清单 SHA、精确来源、证据定位及受影响的主张、图、章节。开源包的双语示例演示两轮更新。研究工程保留原始数据，PaperWriting 保存该轮可复核证据与必要的可绘图数据。

已存在两份章节 TeX、只需展示文字差异时，按[轻量文字对照](review-confirmation.md)生成一份带标记的 TeX 和审阅 PDF，沿用当前稿号。保存新章节修订才使用下述 stage 流程。查询材料使用交接 ID 或清单 SHA；首次设置目录索引与日常取件分别执行。

使用旧章节工具的项目，在用户确认保存章节版本后，Chat 从 `paper_project_context` 取得当前材料交接，核对其来源、实验口径和限制。调用 `paper_section(action="stage")`，在同一 scope 下写 EN/ZH 两份真实正文、材料清单路径和哈希。首次前驱为 `absent`，以后用上个章节 manifest 的 SHA。先检查 dry run，再以 `dry_run=false` 保存新 revision。已有章节和范围之外的正文保持原状。

对返回的两条 tex_paths 分别调用 `paper_build_plan` 和 `paper_tex_build_submit`，传主稿 SHA、输入集 SHA 和稳定请求 ID；凭 build_id 查终态及页图。两个 PDF 必须属于所选章节和同一 revision。调用 `paper_section(action="compare", left_revision=…, right_revision=…)` 查看双语正文差异；保留原版、新版和审阅意见。

默认将本轮需展示的双语章节 PDF 经项目已配置的 Drive 发布或单文件入口交付，记录各语言文件 ID、范围及核验结果。无 Drive 时可在本机继续编辑、构建和查看。文件需要带到 Chat 时先按[文件交付策略](file-delivery-policy.md)处理。仅在该策略允许的兼容模式中，采用已获准的 `paper_section_delivery` 下载/分片入口并核每片和完整 PDF 的 SHA。用 `paper_review_comments` 的 plan 核读发布 PDF 的讨论，collect 收回评论和回复；内嵌标注另走 PDF 解析。将每条意见关联到具体语言和修订，无法定位时留给作者核对。

新会话读项目身份、配置版本和材料状态。网关支持时，带上 `expected_config_revision`。怀疑换绑或出现冲突时重新读取当前绑定及基线，再决定是否续写。旧实例只使用真实发现的工具。
