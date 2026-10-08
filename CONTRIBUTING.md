# Contributing to Mimic SDK

This repository owns the current Mimic extension schema, deterministic generator,
eight native backends, framework adapters, runtime managers and SDK release
tooling. The runtime owns browser semantics, its CDP support registry and binary
artifacts. Change the shared semantic cause in the runtime when a genuine client
reveals a protocol defect; do not patch private framework internals around it.

1. Edit `schema/mimic/protocol.json` for a supported extension contract change.
   Retain exact wire names and model omission independently from null.
2. Run `python generator/generate.py`, then `python generator/generate.py --check`.
   Generated output is not edited by hand. Runtime snapshots are pinned by
   schema bytes/hash and must still generate offline.
3. Add focused conformance or native regression cases, including error and
   ownership boundaries. A compiled backend alone does not qualify an adapter.
4. Test actual native framework objects against a precisely identified runtime.
   Retain runtime binary hash, release/source identity, framework version and
   command/results in a qualification receipt stored in ignored build output or
   CI artifacts.
5. Build and inspect the affected native package. A core-only installation must
   import without its optional framework or any download/startup side effect.
6. Use an explicit SDK release plan for changed packages. Runtime release events
   only qualify compatibility. Never advance embedded pins or package versions
   through that event path.

Use `session.mimic.experimental.call("exactWireLeaf", params)` to investigate a
new contributor command before generated bindings exist. Keep experimental
workflow descriptions here and in API docs; release notes describe the actual
shipped change, not the feature proposal. Promote stable commands into the one
current schema after their runtime semantics and tests are ready.

The root npm manifest and lockfile are generated projections for Git installation.
Edit `node/package.json` and `node/package-lock.json`, then run
`node node/scripts/git-package.mjs`; normal Node release checks reject drift.
The root `prepare` hook compiles the same sources as the native package and copies
its runtime pin. It never downloads or starts Mimic. Registry releases still build
from `node/`; the root package is private. Run `node node/test/git-install.mjs`
in Linux/WSL to prove an installation from a clean temporary Git repository and
the first default runtime launch, without prebuilt files or an executable path.

Run listener/process integration tests inside Linux or WSL with headless loopback
runtimes. Avoid Windows listeners, interactive installers and firewall prompts.
Runtime regressions follow its own AGENTS.md and focused test policy; do not run
its full suite locally.

Public documentation explains usage, contracts, limitations and significant
shipped changes. Keep issue-tracker references, copied tasks, private conversation
links, session diaries and agent progress reports out of source and package
archives. Preserve local evidence privately in ignored `.build/internal-notes/`
or CI artifacts without removing meaningful tests or reference provenance.
