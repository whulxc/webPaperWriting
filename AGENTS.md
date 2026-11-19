# Repository instructions

This is the public webPaperWriting source repository. Read `README.md`, `docs/AI_ONBOARDING.md`, and the affected implementation before editing. Release tag `V0.8.1` corresponds to Python version `0.8.1`; the shared writing skill keeps its own version. `webcodex-paper-studio` remains a protocol/runtime identifier used by existing callers.

Use synthetic project data in examples and tests. Preserve unrelated local changes. Keep secrets, cloud sessions, real papers, personal prompts, project diaries, downloaded reference PDFs, and generated build directories outside this repository.

The Python implementation lives in `paper-studio/paper_studio/`, command wrappers in `management/`, and the shared writing skill in `skills/paper-editor/`. Machine-readable MCP contracts are generated from `paper_studio/contracts.py`; update generated files through the supplied release tooling. Project config determines admitted capabilities and file permissions.

`paper-demo` supplies a small read-only synthetic MCP adapter. `paper-gateway` supplies the project-scoped gateway backed by the user's configured runtime. Use the relevant entry point when validating behavior. A host that can load a skill still needs an admitted tool connection to operate a project.

Keep controlled builds on the Linux isolation backend. Unsupported OS capabilities must return a specific unavailable result. Guard source revisions, paths, output identities, and author confirmation at the existing boundaries. An outcome that becomes uncertain after an external effect needs reconciliation before a retry.

Use the current environment's Python, a dedicated environment when installing dependencies, and explicit output paths for generated artifacts. Check the relevant tests and build the actual distribution when changing packaged resources or console entry points. Add regression coverage for a specific failure the existing suite would miss. Record static, synthetic, packaging, client, and platform validation according to what actually ran.

When changing writing behavior, follow the skill's scoped workflow and references. Cross-project facts come from the user's selected project. This repository ships reusable methods and synthetic demonstrations. Keep detailed installation instructions in `docs/INSTALL.md`, platform evidence in `docs/COMPATIBILITY.md`, and AI setup in `docs/AI_ONBOARDING.md`.
