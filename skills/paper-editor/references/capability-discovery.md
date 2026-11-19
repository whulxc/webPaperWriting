# 统一工具与项目能力发现

一套 Paper Studio 实现可以服务多个独立连接。连接显示名、Runner `client_id`、Paper Studio `project_id`、Native项目ID和物理根分别核实；同名或旧记录不能代替当前身份。技能不保存用户的连接ID、凭据或项目白名单。

## 已有绑定的纯定向取件

已有可信的所选连接/项目上下文，且用户只请求明确版本、修订、语言、角色或图号时，直接用 [精确目标入口](indexed-targets.md)。不先调用宽泛 runtime、`paper_project_inventory`、目录遍历或 Drive search，也不先回顾所有论文版本。核本次返回的项目/配置身份、实际解析修订和目标SHA；身份缺失、过期或冲突才补定点验证。没有启用索引的旧工程报告 `INDEX_NOT_CONFIGURED` 或真实缺口，不把共享技能升级视作生产迁移或授权。

## 新绑定或全面上下文的只读发现顺序

1. 在用户已选择的连接实际调用 `paper_runtime_status`，读取发布/Native版本、Runner、配置身份及授权项目列表。
2. 对列表中的真实项目调用 `paper_capabilities`，再读 `paper_project_context`、PROJECT/HANDOFF/当前状态。最小参数形状为：

```json
{"project_id":"runtime返回且已核验的项目ID"}
```

3. 读取 `tools` 中每项的 `name`、`status`、`reason` 和 `route`；有动作级结果时继续核对当前 action。返回 `verification="configuration_only"` 时，`available` 仅说明配置前提满足，不证明桌面、网络、云端、编译或视觉已成功。
4. 当前宿主若未发现该工具，使用实际 `tool_manifest` 或可见的旧Native发现入口。只按当前返回的Schema调用；连接缓存缺新callable时刷新同一连接，不新建Tunnel或猜工具路由。

能力启用分层核对：先确认 Schema 已注册，再查连接工具准入、当前项目能力与路径范围，最后验证 Provider/Host、工具链和实际执行。`connection_tool_not_admitted` 表示连接准入不足，不等于 TeX 未安装或 Zotero 离线；`not_configured` 应继续定位项目构建配方、引用身份范围或本机连接前提。公共模板只提供可复用能力，不能把其他项目的路径、Collection、凭据或写入授权带到当前项目。Skill 本身不授予工具权限。

状态语义：`available` 为项目当前准入；`not_configured` 为缺少配置或前提；`permission_denied` 为该连接/项目不允许；`unsupported` 为实现明确不支持。状态和真实错误码分别记录，不静默改用其他项目。工具名称在共享源码中存在，不代表该项目具备其写权限。

## 三种调用身份

| 层 | 参数与发现 | 使用方法 |
|---|---|---|
| Host工具 | 统一 `paper_*` Schema及真实Paper Studio `project_id` | 使用直接callable，或当前Schema明确准许的 `call_runtime_tool` 路由 |
| Provider工具 | 先核Runner/provider/tool，再取得不透明binding | `plugin_tool(action="describe", ...)` 后 `action="call"` 只传本次binding与arguments；不要把binding当权限或改指向 |
| Native runtime | Native工具Schema及真实Native项目ID | `read_files`/`edit_project_files`/`project_artifact`等使用其Native `project`；不能用Paper Studio ID代替 |

Provider 精确调用形状：`describe` 使用 `action/runner/plugin/tool`；之后 `call` 的顶层只传 `action/binding/arguments`，不重复 `runner/plugin/tool`，包括不传这些键的 `null` 值。binding 使用本连接本次实际返回值，`arguments.project_id` 仍须为已核实项目。直接 callable 的签名落后于实时契约时，只按当前 `tool_manifest` 明确允许的正式 fallback 调用，不向旧签名强塞新版参数；Provider 管理动作未开放时的 `TOOL_DENIED` 不代表 Provider 离线。

Host `paper_capabilities`、`paper_build_plan`、`paper_source_sync` 等不能因为名字是 `paper_*` 就假装属于Provider。当前精确归属以 `route` 和工具发现为准；`route.host_only_actions` 列出的动作（如 `paper_build_artifact` 的 `image`/`export`）只能用 Host 直接工具，旧版本未返回该字段时同样按此处理。资源链接读取也受本连接已观察的工件身份约束。工具归属只描述可调用能力；普通展示和视觉审阅使用[默认 Drive 路径](build-version-delivery.md#默认drive交付与附件例外)，即使 Host 提供 image/export 也沿用该选择。工具帮助或 usage 提示列出这些动作时，仅说明能力存在；用户提出附件要求时仍先读取顶层 `chat_file_delivery`，按[文件交付策略](file-delivery-policy.md)处理；能力提示本身不会改变连接设置。

配置变化、重连或换绑后重新发现，旧binding与旧revision不沿用。多个连接引用同一密钥存储不合并项目授权；读写根、构建目标、Zotero DOI/Collection、BibTeX写入、Drive项目目录和单篇导入分别以当前项目准入为准。

公共规范发布与论文项目写入是不同授权范围。双库管理入口若只在Codex管理CLI可用，ChatGPT先在实际能力中确认有无相应MCP；没有时提交带基线release/SHA的候选与核验信息，不能虚构工具名或把项目source_sync用作任意公共库写入。ACTIVE/manifest读取与公共规范发布权限分别核验，见[双库协作](dual-library.md)。

## 任务执行

正文写入前实际读取，使用最新numeric `read_revision`精确编辑并读回。构建前取得当前 `paper_build_plan` 的主稿SHA、依赖清单和预期输入快照；图件源只在已授权候选区执行。只读规划、写入、图件生成、TeX构建、版本保存和云端发布是独立动作，不由一次能力查询自动触发。

先查询已有任务和版本，避免重复构建/发布。客户端超时先按原任务ID和稳定request_id对账；支持恢复时按真实Schema关联原失败任务，保留旧终态/日志。未配置或无权限的能力报告明确缺口。正文/数据身份、构建完成、工件字节、云端身份、页面已审和作者接受分别留证。
