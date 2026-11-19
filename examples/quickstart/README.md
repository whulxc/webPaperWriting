# Five-minute synthetic quickstart

Generate two bilingual revisions, evidence hashes and a scoped diff with one command. Every measurement and feedback item in this example is synthetic. Python 3.11 or later and the installed package are sufficient.

Choose an absolute output directory that does not exist. Its parent must already exist.

```sh
paper-demo create --output-root /absolute/new/demo
paper-demo inspect --project-root /absolute/new/demo
```

On Windows, use a native absolute path such as `C:\Users\Public\paper-demo-run`. After a source checkout you can also use `python -m paper_studio.demo` with the same arguments. See [installation](../../docs/INSTALL.md) for installing the package first.

The command returns three successfully exchanged MCP messages and a continuation status of `read`. Both languages point to `rev-002` and have `match` source hashes. Read the following generated files.

| File | Purpose |
| --- | --- |
| `WRITING_BRIEF.md` | Numeric source, bilingual scope and the accepted sentence |
| `materials/measurements.csv` | Three synthetic paired accuracy measurements |
| `manuscripts/rev-001/` | Initial English and Chinese paragraphs |
| `manuscripts/rev-002/` | A localized revision clarifying the percentage-point gain |
| `review/changes.diff` | Exact before/after difference in both languages |
| `review/simulated-feedback.json` | Feedback and acceptance explicitly marked as simulated |
| `PROJECT_STATE.json` | Current revision and source SHA-256 identities |
| `DEMO_REPORT.md` | Expected results and evidence scope |

The illustrative feedback asks the writer to preserve the title, Motivation section and first Results sentence. It changes only Results sentence 2. The corrected sentence names all three fixtures, the two mean accuracies and the gain of 4 percentage points. The shared runtime continuation checker hashes the current files again on every context request.

## Let an AI read the result

Launch this local MCP command from a client that supports stdio. Substitute your absolute generated directory in the command arguments.

```json
{
  "mcpServers": {
    "paper-demo": {
      "command": "paper-demo",
      "args": ["serve", "--project-root", "/absolute/new/demo"]
    }
  }
}
```

If the client does not inherit the package environment, use the full path to the environment's `paper-demo` executable. JSON requires Windows backslashes to be escaped. Client-specific configuration belongs in that client's settings.

A reusable prompt for the connected AI follows.

> Read the synthetic demo's runtime status and project context. Show the current bilingual revision and source hash status. Read the brief, measurements and scoped diff. Explain how the second Results sentence changed and identify which content stayed identical. Keep the feedback's simulated status visible.

The adapter uses the package's MCP wire implementation and real continuation checker with a bounded reader restricted to its allowlisted synthetic files. It exposes `paper_runtime_status`, `paper_project_context` and `paper_read_files` for the generated fixture files. Its status explicitly reports a local synthetic adapter and an unconfigured Native connection. Writing, cloud delivery and PDF compilation are outside this fixture's tool catalog. Use the [AI onboarding guide](../../docs/AI_ONBOARDING.md) to connect your own authorized project.

## Inspect a source conflict

In the generated example directory, manually change the English `rev-002` paragraph. Run `paper-demo inspect` again. The current source hash now differs from the registered hash and the continuation status becomes `source_conflict`. Both revision directories are preserved. Start again with a new output directory to regenerate the original fixture.

## Validation scope

Tests exchange framed JSON-RPC over in-memory streams, exercise this adapter with temporary synthetic projects, and verify scope, hashes and refusal to overwrite. Native operating-system installation, real accounts, cloud services, TeX, physical devices and human writing acceptance are outside these tests. For a controlled TeX build after installing the Linux dependencies, see [the bilingual sections example](../bilingual_sections/README.md).
