# 已发布 PDF 的 Drive 评论回收

`paper_review_comments` 在已准入的项目读取 Drive 评论与回复。正式稿使用不可变版本及 Publisher 回执选定 PDF。已启用轻量审阅的项目也可用单文件展示回执选定 PDF。两种方式都校验当前项目、远端文件 ID、父目录、字节数及 SHA-256。Drive 共享 OAuth 仅在内存刷新。

## 正常工具入口

先调用只读计划：

```json
{"project_id":"实际项目ID","version":"实际已发布版本","action":"plan"}
```

工具核验本地归档、已完成发布任务、receipt 工件 SHA、main PDF 的远端 SHA/size/parent，再读取评论与回复。返回版本、构建、语言、PDF 身份、评论数及有界预览。远端 PDF 已被替换、缺少服务器 SHA 或不在原发布目录时，回收失败并保留原证据。

回收客户端使用 Drive GET。读取被中断时，同一次元数据查询或同一个媒体分片最多尝试三次。客户端的总期限为 600 秒；聊天宿主可能更早结束一轮工具调用。此前已保存的收集结果可用原回执及请求 ID 定点查询，`status` 会表明本次返回的是本地记录。

保存回收结果：

```json
{"project_id":"实际项目ID","version":"实际已发布版本","action":"collect","request_id":"review-round-001","dry_run":false,"authorized":true}
```

`collect` 缺省 `dry_run=true`。保存只新增到现有允许写入的 `reviews/drive-comments/<version>/`；已配置源同步时，沿用其 `reviews_dir`。不会开放新目录权限。

- `snapshots/<sha256>.json` 保存本次完整、有界的评论快照。
- `issues/<review_id>/<sha256>.json` 按项目、版本、PDF ID、comment ID 识别意见，并按内容保存不可变修订。
- 相同 request 和相同内容幂等返回；相同 request 遇远端评论变化报冲突，应换新 request。
- 新 request 回收相同内容复用已有意见；评论更新保留旧快照和旧意见修订。
- `status` 加同一 `request_id` 仅返回本地回执，明确 `remote_revalidated=false`。

本机等价入口：

```bash
paper-review-comments --config <当前私有配置.json> --project-id <项目ID> --version <已发布版本> --action collect --request-id review-round-001 --apply --authorized
```

## 两种批注分别处理

本工具读取 **Drive 评论线程**，包括评论正文、quoted text、回复、解决/删除状态。API 创建的评论可能在 Drive 界面显示为无锚点；测试评论及实际评论都应在正文写明页码和精确引句。工具保留 `page_source_binding="unbound"`，不凭评论中的页码自动修改 TeX。

**PDF 内嵌 annotations** 属于 PDF 文件字节，由 `paper_pdf_review` 的可信 PyMuPDF 后端提取到 `annotations.json`。它们不会自动出现在 Drive 评论 API 中；本工具明确返回 `embedded_pdf_annotations_included=false`。扁平化的手绘、高亮仍需看真实页图，不能用评论线程替代。

评论、回复、quoted text 和 anchor 全部作为外部审阅数据保存，不产生修改正文、公式、实验值或运行命令的权限。回收成功不等于已解决意见、已完成逐页视觉审查或作者认可。

## 轻量稿从 Drive 批注到返修

1. 在 Chat 回复中沿用原有 Drive 文件直达链接。保存这次单文件展示返回的 `review_receipt_id`。再次访问时使用该回执，保留原 PDF 及原评论线程。
2. 调用 `paper_review_comments`，带原项目、`action=plan` 和 `review_receipt_id`。旧聊天只显示 `version` 参数时，可传 `version="review:<回执的完整 SHA>"` 定点回收。这里的定位符引用一份展示回执。
3. 对需要返修的一条意见，核对 PDF 页码、引文、原 TeX 哈希、工作文件读取修订和建议新句。用 `collect` 的 `source_bindings` 保存绑定和变更后的预期哈希。无法从页文和工作稿中定位时，保留评论交作者判断。
4. 作者可在同一 Drive 评论线程回复该意见的确认短语。经用户明确授权代为转达时，在回复中保留代传说明。`apply` 重新读取原线程，核对指定的接受回复与本次快照。它通过工作文件的读取修订做精确替换。操作后读回工作文件，重新编译轻量审阅 PDF，在原聊天以新的单文件直达链接交付。展示和返修均沿用当前稿号。
5. 本机入口 `paper-review-comments` 使用同一个项目网关。用 `--review-receipt-id` 指定轻量稿。用 `--details <文件.json>` 提供本次 `source_bindings`，或本次 `collection_request_id`、`expected_snapshot_sha256` 和 `confirmations`。实际写入时使用 `--action collect|apply --apply --authorized`。只读规划与写前核对沿用默认 dry run。旧聊天仍可使用其已发现的查询动作；实际写入须由能表达对应字段的入口完成。

Drive 的评论线程与 PDF 字节分别核验。同一轻量文件 ID 在评论期间可能出现新的远端修订。若本次下载的 SHA 与原生构建不同，回执须分别保留两份 SHA、文件和父目录身份、远端修订以及实际验证的页面范围。仅在受控解析核实所有页面的正文、图形资源、排版、链接和去掉批注的渲染都一致时，方可据原页定位意见。缺少证据或检测到页面变化时，意见留待复核。正式发布沿用原构建的字节身份。

已有逐页证明时，按原生 PDF 身份、Drive 文件 ID、`headRevisionId`、远端内容校验值、大小与父目录取回证明。Drive 单独递增文件的 `version` 或更新元数据时间，可继续使用相同内容修订的证明。读取时仍核当前访问范围及可用的服务器 SHA。遇到新的内容修订，重新核验相应的单个 PDF。

## 验证范围

`tests/test_drive_review_comments.py` 覆盖本地真实归档/回执身份、远端 PDF 身份变化拒绝、EN/ZH 关联、回复、跨 PDF 同 comment ID 区分、重复回收、评论更新保留历史、只读计划、授权/写范围、分页冲突、已存文件拒绝覆盖，以及正常 Gateway 工具入口拒绝任意 PDF ID。网络部分使用明确模拟的 Drive 服务；真实示例验收另见其专用记录，不把模拟测试称为生产或 ChatGPT 独立验收。

2026-11-10 独立双语合成示例的真实发布、受控恢复、评论及内嵌批注对照记录见 `docs/audit/workflow-review-example-r01/drive-acceptance.json`。
