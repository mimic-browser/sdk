# Mimic SDK

SDK source is separate from the browser runtime. Public materials are English.
Preserve actual framework Browser/Context/Page/Locator objects. Follow spec/
contracts, exact artifact pins, raw error semantics and per-language conventions.
Generated files belong to generator/; do not patch them manually.

Keep work local unless the user explicitly authorizes pushes, repository creation
or package publication. Do not start headful processes or anything requiring user
interaction. Run listener/integration tests in Linux or WSL; Windows tests must
not open listeners. Do not run the full runtime test suite. Runtime changes
follow that repository's AGENTS.md. Reuse compiled tests when source is unchanged.

Keep source readable. Format authored JS/TS with Prettier 3.6.2 and Go with gofmt.
Test actual behavior and failure/ownership boundaries, not only generated text.
No native package or adapter is qualified merely because its source compiles.

## Public documentation and private evidence

- Public files and filenames must not contain issue-tracker task identifiers,
  issue URLs, task UUIDs, copied task exports or links to private conversations.
- Do not publish personal browsing/session diaries, daily work logs, prompts,
  assistant/subagent progress reports or task-specific execution summaries.
- Public documentation describes current usage, supported behavior, architecture
  and limitations. Release notes describe significant shipped changes, without
  internal planning history or task-report scaffolding.
- Retain task notes, local qualification receipts, machine-specific diagnostics
  and session reports in ignored `.build/internal-notes/` or CI artifacts. Check
  package file lists so private evidence cannot enter a distribution archive.
- Preserve useful failures, frozen reference captures and their oracle provenance.
  Privacy cleanup must not weaken tests, support registries or release integrity
  checks. Keep canonical contracts, artifact hashes and reproducible test methods
  public when they explain supported behavior; archive private context separately.
