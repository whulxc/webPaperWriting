# Install webPaperWriting

V0.8.1 ships a Python wheel, source archives and the shared web论文写作 skill. Python 3.11 or newer is required. Choose the environment that owns the paper workspace. Every operating system uses its own Python environment.

[Platform and verification matrix](COMPATIBILITY.md) · [AI-assisted setup](AI_ONBOARDING.md)

## Linux and WSL 2

Extract the source release and enter its root, which contains `pyproject.toml`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/paper-bootstrap --help
.venv/bin/paper-handoff --help
.venv/bin/paper-skill --output ./dist/skills
```

A downloaded wheel can replace `.` in the install command with its actual filename. Place your own paper workspace outside the software checkout. Use an absolute project root and a project identifier chosen for that workspace.

```sh
.venv/bin/paper-bootstrap --project-root /absolute/path/to/research --project-id example-paper
```

In WSL 2, execute these commands inside the chosen Linux distribution. Use Linux paths in its configuration. Start with a workspace on the distribution's Linux filesystem when Linux isolation and private POSIX permission checks are needed. A Windows path and its WSL mount path refer to one workspace but use different path syntax. Keep the WSL Python environment within that distribution.

## Windows PowerShell

Extract the source release and enter its root. Use the interpreter and command entrypoints explicitly, which avoids requiring PowerShell activation scripts.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\paper-bootstrap.exe --help
.\.venv\Scripts\paper-handoff.exe --help
.\.venv\Scripts\paper-skill.exe --output .\dist\skills
```

Use your own absolute Windows path when initializing a paper workspace. Bootstrap, material handoffs, text-review source preparation and skill packaging are the native Windows entrypoints targeted by V0.8.1. Complete Gateway execution runs in WSL 2 or on a Linux host. The protected filesystem layer requires POSIX primitives and returns `PLATFORM_UNSUPPORTED` when they are absent. Follow the WSL instructions above for that execution environment.

## macOS

Install Python 3.11 or newer using your preferred Python distributor. Extract the source release and enter its root.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/paper-bootstrap --help
.venv/bin/paper-handoff --help
.venv/bin/paper-skill --output ./dist/skills
```

macOS targets the portable CLI, skill tooling and POSIX Gateway configuration. Controlled TeX, figure and PDF workers execute on a Linux build host. Configure the project connection to the host that owns that workspace and toolchain. A local path is interpreted by its execution host.

## Skills and AI clients

The `paper-skill` output includes the shared skill and a ChatGPT-importable package. For a new Codex installation, copy the complete `skills/paper-editor` directory into that client's user skill directory, or use `paper-skill --install` with the explicit destination. Review an existing same-name skill before replacing it.

Run `paper-gateway --config /absolute/private/instance.json` on its POSIX host for a local stdio MCP client. A client connection must register its command, arguments and private configuration path. Use the provided configuration templates and the [AI setup guide](AI_ONBOARDING.md) to determine the project and admitted capabilities. Credentials are supplied by the user's own WebCodex installation. They remain outside the public checkout and release assets.

ChatGPT uses the user's own WebCodex connection and its supported public transport. Shared skill installation and runtime connection registration have independent status. The upstream runtime and authentication setup are described by [WebCodex](https://github.com/yyjeqhc/webcodex).

## Controlled builds and optional services

The current isolated worker uses Linux namespaces, `bubblewrap`, a working user-level `systemd` bus and cgroup limits. It requires a complete Python prefix and a compatible TeX Live tree. Its current worker layout uses `bin/x86_64-linux`. WSL 2 requires systemd enabled in the distribution and the same isolation prerequisites. A build request is refused when these requirements are absent.

Google Drive publication, Zotero integration, PDF backends and Windows PowerPoint automation are optional capabilities enabled in each user's private project configuration. Their separate dependencies, credentials and permissions must be supplied by that user. Refer to [compatibility](COMPATIBILITY.md) for the exact release validation status.
