# Node and Python integration checks

Run checks from the corresponding package directory. Unit checks cover generated
wire values and runtime selection; integration checks exercise the real client,
process ownership, raw protocol routing and Context setup.

| Scope                    | Node                               | Python                                                 |
| ------------------------ | ---------------------------------- | ------------------------------------------------------ |
| Build and unit checks    | `npm run build && npm test`        | `python -m unittest discover -s tests -p test_unit.py` |
| Raw transport            | `node --test test/transport.mjs`   | `python tests/transport.py`                            |
| Ordinary client flows    | `node --test test/integration.mjs` | `python tests/integration.py`                          |
| Managed Context bridge   | `node --test test/bridge.mjs`      | `python tests/bridge.py`                               |
| Synthetic capture        | `node --test test/media.mjs`       | `python tests/media.py`                                |
| Python Pyppeteer adapter | —                                  | `python tests/pyppeteer_integration.py`                |

Use an isolated environment for Pyppeteer because its WebSocket dependency
differs from modern test tooling. Tests that need an explicit development runtime
or synthetic provider fixture must receive those executables through their
documented environment variables. Runtime and fixture listeners run headless on
ephemeral loopback ports.

The concurrent Node/Python installer check uses `python3` by default. Set
`MIMIC_TEST_PYTHON` to the Python environment containing the SDK when needed.

Transport checks preserve omitted values, explicit null, session identity and
protocol error code/message/data. They cover concurrent replies, cancellation,
timeouts and closed-transport rejection. Installer checks cover exact artifact
pins, shared-cache races, integrity failures, offline reuse and active leases.

Capture checks use deterministic camera and microphone providers rather than
hardware. They cover private source routing, public identity, permissions,
factory failure, reentrant close, missing sources, clones and capture teardown.
See the [media contract](../docs/media.md).

The default runtime and current client limitations are listed under
[supported integrations](../compatibility/README.md). A passing package build
alone is not a compatibility result. Retain exact runtime, client and artifact
identities with check results in ignored `.build` output or CI artifacts; keep
machine-specific diagnostics and development-session reports out of public docs.
