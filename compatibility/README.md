# Supported integrations

Mimic SDK returns the selected automation framework's own Browser, Context,
Page and Locator objects. Framework versions and runtime capabilities both
determine compatibility; an available SDK method does not imply runtime support.

| Language                | Native integration                        | Client version  |
| ----------------------- | ----------------------------------------- | --------------- |
| JavaScript / TypeScript | Playwright; Puppeteer                     | 1.63.0; 25.10.0 |
| Python                  | Playwright sync/async; optional Pyppeteer | 1.63.0; 2.0.0   |
| .NET                    | Playwright; PuppeteerSharp                | 1.63.0; 25.12.0 |
| Java / Kotlin           | Playwright Java                           | 1.63.0          |
| Go                      | Rod; chromedp                             | 0.116.2; 0.15.1 |
| Rust                    | chromiumoxide                             | 0.9.1           |
| Ruby                    | Ferrum                                    | 0.18.0          |
| PHP                     | chrome-php                                | 1.16.0          |

## Runtime requirements

The bundled runtime pin is **v0.2.2**. Its exact manifest and archive/executable
hashes are in [`release/runtime-lock.json`](../release/runtime-lock.json).
Ordinary Playwright, Puppeteer, Rod and PHP examples can use that default.
Managed environment-profile helpers require `Mimic.configureContext`, which is
not available in v0.2.2. PuppeteerSharp, chromedp, chromiumoxide and Ferrum also
require runtime fixes awaiting a compatible release.

These integrations therefore have different release readiness. A development
executable can exercise pending behavior, but it does not qualify the unchanged
official pin. Publication requires a compatible official runtime, an intentional
default-pin update and qualification of the selected packages against that
exact executable. See [release requirements](../release/README.md).

Runtime artifacts support Windows x64 and Linux x64 with glibc 2.39 or newer.
Other hosts fail before downloading. SDK language support does not extend the
runtime's platform support. All SDK-owned launches are headless and bind a
loopback address; `connect` preserves the externally managed process.

## Capability boundaries

Framework APIs retain their original naming and object types. Unsupported
browser behavior remains visible rather than being emulated by SDK wrappers.
The runtime's `internal/cdp/protocol_support.json` is authoritative for semantic
CDP support. The SDK [schema](../schema/mimic/protocol.json) describes wire shapes.

[Media factories](../docs/media.md) separate private capture sources from public
device identities and configure the owning Context before user Pages exist.
Synthetic capture checks establish source routing and lifecycle behavior;
physical camera and microphone driver compatibility requires separate checks.

Installer verification, raw protocol conformance, real-client integration and
native package installation are separate release checks. Their receipts belong
in ignored build output or CI artifacts, while this page describes the current
support boundary. See [contributing](../CONTRIBUTING.md) for the workflow.
