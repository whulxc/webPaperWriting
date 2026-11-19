# Contributing

Contributions that make the first successful writing workflow easier to reproduce are welcome. Small fixes, synthetic examples, clearer AI setup instructions, and platform-specific findings all help.

Read `AGENTS.md` before changing the code. Install from the source root in a dedicated Python environment using `python -m pip install ".[test]"`. Keep generated artifacts in a new output directory outside the source tree. The repository includes fixture tests that can run without private accounts; choose the tests relevant to the change and record their actual result.

For a bug report, include the release tag, Python and OS versions, the command or MCP tool, and a minimal synthetic reproduction. Inspect logs before sharing them. Account credentials, private project paths, manuscript text, and review comments belong in the user's private environment.

For a pull request, describe the behavior being fixed, the final change, and validation. Use explicit fixtures for effects involving remote services. Record platform installation or real-client results only when those operations were actually performed. Contribution does not require access to a maintainer's live paper project.

Keep the same current source revision, project boundaries, and retry semantics across CLI, MCP, and asynchronous jobs. New behavior should remain callable through its normal public entry point. Retain existing caller behavior unless the change intentionally updates that contract.

Original contributions are submitted under the repository's Apache-2.0 license. Identify adapted code, assets, or dependencies and preserve their applicable notices. Documentation changes should update both homepage languages when user-facing behavior changes.
