# AI onboarding

Use this guide when an AI assistant installs webPaperWriting, connects a paper workspace, or resumes work. The public release tag is **V0.8.1**, the Python distribution version is `0.8.1`, and the writing skill has its own version. Development rules for changing this repository are in `AGENTS.md`.

## Start with the offline example

Read `docs/INSTALL.md` and `docs/COMPATIBILITY.md` for the actual operating system. Use the current environment's Python executable and a new absolute output directory chosen for the example.

```text
python -m pip install .
paper-demo create --output-root /absolute/new/paper-demo
paper-demo inspect --project-root /absolute/new/paper-demo
```

For a wheel installation, replace `.` with the downloaded wheel path. The example writes synthetic inputs, two manuscript revisions, simulated feedback, and `PROJECT_STATE.json`. Read `DEMO_REPORT.md` and `review/changes.diff` before describing its outcome. All experimental values and acceptance decisions in the demo are synthetic.

Configure a local MCP client with the `command` and `args` from `integrations/mcp/stdio-demo.json`, replacing the command with your installed executable path and supplying the absolute project path. The example adapter runs as follows.

```text
paper-demo serve --project-root /absolute/new/paper-demo
```

Use the host's MCP configuration format. `stdio` clients launch this command as a subprocess. Protocol messages use standard output, diagnostics use standard error, and the host manages process lifetime. [MCP transport specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)

Discover the example's three read-only tools through `tools/list`. Call `paper_runtime_status`, `paper_project_context`, and `paper_read_files` using their returned input schemas. Check that the project is `synthetic-quickstart`, inspect its continuation record, and read the requested current manuscript and evidence. The example is a narrow synthetic adapter. The real project gateway and its full tool catalog are described below.

## Connect a research project

Identify the user's existing research root, authoritative paper directory, and allowed operations. Read its existing project instructions and handoff before creating anything. A usual paper workspace is `<research-root>/PaperWriting`; an established workspace can keep its current location.

Install the release and generate or install the writing skill with `paper-skill`. For a new paper workspace, use `paper-bootstrap --help` to supply the actual root and project ID. Existing papers use explicit mappings to their own sources and build targets. The deployment configuration belongs in the user's private configuration directory.

Start with the [read-only instance template](../instances/instance.template.json), replacing every placeholder with your own registration. The [stdio project template](../integrations/mcp/stdio-project.json) is a template for launching `paper-gateway --config <absolute-private-instance.json>`. The gateway uses the configured WebCodex Native runtime, authorized project roots, and trusted provider. Follow the matching WebCodex release's configuration guide to install that runtime and register the project. The public release does not include a personal Runner registration or cloud credentials. [WebCodex upstream](https://github.com/yyjeqhc/webcodex)

For ChatGPT, the user configures their own supported MCP connection or tunnel and installs the skill in that host. For a local MCP client, launch the stdio gateway using that client's configuration syntax. A skill package supplies writing instructions; tool discovery comes from the configured server.

## Discover before editing

The full catalog is generated from the implementation in `paper-studio/paper_studio/contracts.py` and published at `integrations/mcp/tools.schema.json`. At runtime, call `tools/list` to discover the host catalog and `paper_capabilities` to determine which tools and actions the project admits. Use the returned input schemas for arguments.

1. Read `paper_runtime_status` to discover authorized project IDs and the current runtime.
2. Pass the selected `project_id` to `paper_capabilities` and `paper_project_context`. Keep the returned configuration revision when an operation accepts `expected_config_revision`.
3. Read the project brief, continuation record, current source, and relevant evidence. When `continuation.status` is `source_conflict`, show the conflicting source identities and resolve the baseline with the author before a dependent edit.
4. Use the current source revision and required hashes when staging an authorized change. Poll the returned build or request ID to reconcile asynchronous outcomes.
5. Report source changes, completed builds, review artifacts, and author acceptance according to their actual receipts.

| User intent | Tool route when admitted |
| --- | --- |
| Find a registered version or figure | `paper_index_get` with the explicit version, revision, language, and role |
| Read current material | `paper_material_get` or project-scoped `paper_read_files` |
| Save a bilingual section candidate | `paper_section` with its material and predecessor identities |
| Review selected proposed edits | `paper_review_proposal`, including the author's decisions before apply |
| Build a trusted TeX entry | `paper_build_plan`, then `paper_tex_build_submit`, then `paper_build_status` |
| Retrieve a completed artifact | `paper_build_artifact`, following the project's delivery policy |
| Verify references | scoped BibTeX/Zotero tools and original source evidence |
| Deliver a review file to Drive | `paper_publish_to_drive` with the exact completed build identity |

Linux/WSL supplies the controlled build backend. Windows and macOS use their documented core capabilities and a configured Linux build host for controlled execution. A capability reported as configured still requires the requested operation's actual completion evidence.

## Keep writing scoped

Use the skill's `references/editorial-review.md` for paragraph responsibilities, wording, figures, and author feedback. For a small revision, locate the replaceable text and preserve already accepted material. For a new paper or major restructure, read `references/full-workflow.md` and prepare a project brief, evidence audit, and story blueprint before drafting.

A useful user request gives the intended reader, target section, baseline, and deliverable. For example, ask to revise a results paragraph against the current table, retain the accepted opening sentence, and produce a candidate with a short explanation of the changes. Existing project facts supply the scientific substance.

After an authorized revision, update the project's existing continuation entry with its actual source paths, hashes, decisions, and next task. A new conversation reads that entry and verifies the live sources. Keep credentials and personal account metadata in private configuration.

## 中文交接提示词

请按本仓库的 AI 接入指南检查我的操作系统和 Python 环境，先在我指定的全新目录运行合成示例。读取示例的当前稿、证据与接续记录，告诉我两轮修订改了哪些内容。接入真实论文时，先核对我选定的工程、当前稿源和允许修改的范围，再按 web论文写作完成本轮任务。构建、批注接受和对外交付各自记录实际结果。
