# MCP 调用与同会话连续性

## 连接和项目识别

- 用户在宿主中选择按部署登记的 MCP 连接名；每个连接可能对应不同 Tunnel、Gateway、Runner 与项目。**Skill 不内嵌 Tunnel ID、Runtime API Key、PAT 或地址**。
- 首轮优先使用统一 `paper_runtime_status`，再对返回的真实 `project_id` 调用只读 `paper_capabilities` 与 `paper_project_context`；旧接口用实际 `runtime_status`/project discovery/`read_files` 接续。读取根目录、候选目录、read/write/build scope、实际发布版本与配置身份。按 [能力发现](capability-discovery.md) 路由，不把 Host 工具、Provider 绑定与 Native 项目 ID 互换。无法核实项目身份时停止写入。
- 如果同 Runner 有多个项目注册入口，区分项目角色（事实只读、候选可写、构建输出），按明确的项目 ID 路由；同名目录和历史离线记录不是身份。
- 工具元数据里出现能力，不等于身份实际获授权。必要时在明确允许的测试目录做安全小范围验收；真实稿件不用于试探写权限。
- 用户另一个 Chat 或本地管理系统把该 Tunnel 从 A 切换到 B 后，本 Chat 的项目上下文可能过期。若状态与先前不符，停止当前写入，重新读取项目资料，不把 A 的内容写入 B。

## 专用普通 Chat

优先在每篇论文的普通 ChatGPT 写作聊天中选择一个目标 App/Tunnel，首次核其真实 `project_id`。同一聊天后续沿用已核身份，用户可省略重复 `@` 和 ID，工具调用继续携带所需身份。每次写作、续写、翻译、润色或返修前，读取并应用本包[反防御写作原文](anti-defensive-writing.md)，按 [Chat 写作入口](chat-writing.md#普通-chat-的项目与写作风格)接续。纯取件走定向入口。Codex 继续使用材料入口。

## 连续工作协议

- 在本会话首次明确启用 Skill 后，该会话后续科研论文相关请求默认为延续；继承已确认的目标期刊、项目 ID、版本、稿件范围与图件风格，除非用户修改它们。
- 若只说“继续”“再优化一版”“图1改一下”，以本会话最近一次**真实构建和版本记录**为基准，而不是依据想象的文件状态。
- 用户另开 Chat 时必须重新选择连接并启用 Skill；不得认为 Skill 在所有新 Chat 自动生效。
- 用户无需每次再次 `@` 或重述项目 ID。当前宿主未保留工具访问时，先给出重新选择原 App/Tunnel、核其真实项目身份的恢复步骤；报告本轮实际可用范围。
- 多会话同时改同一项目时使用 git/worktree、期望 revision、构建快照和写前冲突检查，任何合并冲突必须显式处理。
- 用户指定“只读/只规划”时，不得写入、构建或发布。构建器是否纯只读应依据实际副作用判断。

## 失败处理

- MCP/Tunnel 离线：准确报告失败，不伪造数据；可先处理用户已提供的文字材料。
- 某工具返回 `permission_denied` / `not_configured` / `unsupported`：保留实际原因，不跨项目切换或使用更高权限秘密；可以用当前项目已有安全等价工具。能力发现本身只读，不注册新服务或授予 scope。
- 构建超时：优先提交异步任务并查询任务 ID，不因连接超时自动重复启动相同任务。
- 产物 ResourceLink 仅表明链接已生成，未必代表 ChatGPT 取得完整字节。
