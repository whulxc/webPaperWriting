# WRITING_BRIEF

> 这是写作决策和跨宿主交接文件。它引用项目索引，不复制原始实验数据、全文 PDF、凭据或长日志。
> 若项目已有等价写作简报，请扩展原文件并在这里记录其真实路径。

## 1. 任务身份

- project_id:
- project_root:
- paper_identity:
- mode: REVISION | REBUILD
- baseline_version:
- baseline_source_sha256:
- work_source: 指向现有状态中的可编辑稿或具体候选
- latest_review_delivery: 上次实际交付链接与版本
- author_accepted_baseline: 最近实证，未知时标记 unknown
- target_venue:
- target_article_type:
- language_scope: en | zh | bilingual
- brief_status: draft | evidence_ready | writing_ready | review_ready | delivered
- owner_decision_points:

## 2. 真实材料入口

| 主题 | 真实索引或文件 | 版本 / SHA | 读取范围 | 备注 |
| --- | --- | --- | --- | --- |
| 科研主线 |  |  |  |  |
| 方法和设置 |  |  |  |  |
| 实验结果 |  |  |  |  |
| 图源和图表数据 |  |  |  |  |
| 正文和 TeX |  |  |  |  |
| Zotero / BibTeX |  |  |  |  |

材料类型标记为 observed、confirmed、candidate、unknown。不能从历史草稿直接推断当前真值。

## 3. RESEARCH_PREFLIGHT

| 角色 | DOI / 题名 | 元数据来源 | 实际阅读范围 | 图号 / 页码 | 证据等级 | 可迁移原则 |
| --- | --- | --- | --- | --- | --- | --- |
| Scientific Story Reference |  |  |  |  |  |  |
| Experimental Narrative Reference |  |  |  |  |  |  |
| Scientific Figure Reference |  |  |  |  |  |  |

其他用于 Introduction、Related Work、Discussion 的文献继续登记在项目文献索引或 Zotero，不把三篇参考论文当作文献检索上限。

## 4. Paper-level story

- 核心科学问题：
- 已确认研究缺口：
- 主要创新边界：
- 核心科学主张：
- 证据摘要：
- 不可声称的内容：
- 仍需验证的主张：

## 5. Section-level blueprint

| 章节 | 目标问题 | 核心主张 | 支撑证据 | 计划图表 | 关键过渡 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Introduction |  |  |  |  |  |  |
| Methods |  |  |  |  |  |  |
| Results |  |  |  |  |  |  |
| Discussion |  |  |  |  |  |  |
| Conclusion |  |  |  |  |  |  |

## 6. Figure–Claim–Results storyboard

| Figure ID | 要证明的主张 | Results 小节 | 子图顺序 | 数据 / 模型来源 | 比较对象 | 图注要点 | 编辑源 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Fig. 1 |  |  |  |  |  |  |  |  |
| Fig. 2 |  |  |  |  |  |  |  |  |

定量图必须关联真实数据、冻结结果或可复核的分析脚本。方法示意图必须注明数据证据与设计方案的边界。

## 7. Paragraph–Sentence plan

| 位置 | 段落问题 | 核心句功能 | 证据或引用 | 与前后段关系 | 待写 / 待审 |
| --- | --- | --- | --- | --- | --- |
|  | Context |  |  |  |  |
|  | Gap |  |  |  |  |
|  | Claim |  |  |  |  |
|  | Evidence |  |  |  |  |
|  | Interpretation |  |  |  |  |
|  | Transition |  |  |  |  |

这些是功能标签，不是固定句数模板。

## 8. Evidence and citation gaps

- 缺少的实验事实：
- 缺少的图表或源数据：
- 未解析或未支持的引用：
- 需要作者决定的科学取舍：
- 不应由 ChatGPT 自行补写的内容：

## 9. Chat / Codex / WebCodex handoff

### Codex 已完成

- 材料整理：
- 文献预研：
- Zotero / BibTeX：
- 事实和哈希核验：
- 项目连接实际项目与 project_id：
- 已允许的写入范围：

### ChatGPT 待完成

- 可选叙事辅助技能及实际使用范围：
- 可选图件辅助技能及实际使用范围：
- 反防御写作：
- 章节或图件任务：
- 需要作者确认的事项：

### webPaperWriting / 项目连接交付

- 当前构建入口：
- 预期引擎：
- 预期输出：
- Drive 交付策略：
- 不允许的路径或操作：

## 10. Revision and delivery record

- 父稿 / 独立论文身份：
- 本轮变更：
- 影响章节和图件：
- 构建 ID：
- EN PDF / SHA / 页数：
- ZH PDF / SHA / 页数：
- Drive 文件 ID / URL：
- 科学审查状态：
- 视觉审查页码：
- 作者决定：
- 未决阻断项：
- 最后更新：

沿用已有批注编号与项目记录。下表用于重要作者取舍和复发问题，可链接至现有记录。

| 原意见或样例 | 适用稿件与位置 | 作者决定 | 实际改动及候选位置 | 复核结果 |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

写作审阅、实际页面检查和作者接受分别记录。一次轻量修订可直接复用现有批注或候选条目。

