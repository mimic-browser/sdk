# Mimic SDK

Use the browser automation API you already know, with a managed Mimic runtime
and typed Mimic capabilities beside it. Browser, Context, Page and Locator
objects come from the selected framework. The SDK does not imitate their APIs.

Install the SDK directly from [GitHub](https://github.com/mimic-browser/sdk).
Registry packages are not published yet; each language guide shows a source
installation using its native package manager or build tool.

## Happy path

Install the [Node package](node/README.md):

```sh
npm install git+https://github.com/mimic-browser/sdk.git
```

Use the client already installed in your project. For the Playwright example
below, install `playwright-core@1.63.0` if needed; Puppeteer uses the separate
`puppeteer-core@25.10.0` client and `mimic-browser/puppeteer` import. Both clients
remain optional. The core package never installs a framework for you.

Git installation compiles the package automatically. The eventual npm package
name is `mimic-browser`.

```typescript
import { launch } from "mimic-browser/playwright";

const session = await launch();
try {
  const context = await session.newContext();
  const page = await context.newPage();
  await page.goto("https://example.com");
  console.log(await page.title());
  console.log(await session.mimic.getVersion());
} finally {
  await session.close();
}
```

The first launch installs the exact verified runtime into a persistent OS cache.
Later launches verify and reuse it offline. Each launch owns a separate browser
process. Importing a package neither downloads nor starts anything.

For [Python](python/README.md):

```sh
python -m pip install "mimic-browser @ git+https://github.com/mimic-browser/sdk.git#subdirectory=python"
python -m pip install playwright==1.63.0
```

```python
from mimic.playwright.sync_api import launch

with launch() as session:
    context = session.new_context()
    page = context.new_page()
    page.goto("https://example.com")
    print(page.title())
    print(session.mimic.get_version().version)
```

Prefer managing the runtime yourself? Ordinary CDP remains a first-class path:

```typescript
import { chromium } from "playwright-core";

const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
const context = await browser.newContext();
const page = await context.newPage();
await page.goto("https://example.com");
await context.close();
await browser.close(); // Disconnects this externally connected Playwright client.
```

## Choose your language and client

| Language                | Native package / module                             | Optional framework integration                        |
| ----------------------- | --------------------------------------------------- | ----------------------------------------------------- |
| JavaScript / TypeScript | [`mimic-browser`](node/README.md)                   | Playwright, Puppeteer                                 |
| Python                  | [`mimic-browser`, import `mimic`](python/README.md) | Playwright sync/async; optional Pyppeteer             |
| C# / .NET               | [`Mimic.Sdk`](dotnet/README.md)                     | Playwright, PuppeteerSharp, separate adapter packages |
| Java / Kotlin           | [`io.mimicbrowser:mimic-sdk`](java/README.md)       | Playwright; Kotlin uses the JVM API                   |
| Go                      | [`github.com/mimic-browser/sdk/go`](go/README.md)   | Rod, chromedp subpackages                             |
| Rust                    | [`mimic-sdk`](rust/README.md)                       | chromiumoxide feature                                 |
| Ruby                    | [`mimic-browser-sdk`](ruby/README.md)               | Ferrum, optional require                              |
| PHP                     | [`mimic-browser/sdk`](php/README.md)                | chrome-php, optional adapter                          |

Framework dependencies are explicit and optional. Their genuine APIs retain
their own conventions; Mimic extensions use camelCase in JS/Java/PHP,
snake_case in Python/Ruby/Rust, and exported PascalCase in Go/.NET.

## Reproducible runtime management

- Pin an exact release or commit a runtime lock with original manifest bytes,
  source identity and archive/executable hashes. No `latest` substitution.
- Use `MIMIC_RUNTIME_VERSION`, `MIMIC_EXECUTABLE_PATH`, `MIMIC_RUNTIME_DIR`, and
  `MIMIC_DOWNLOAD=0` with the shared [selection rules](spec/runtime-manager.md).
- Preinstall verified archives for offline CI. All languages share the cache,
  installation lock, receipt and lease formats.
- `launch` owns startup, readiness and teardown. `connect` attaches to an
  existing endpoint and preserves its process and other clients.
- Inspect and verify installations; prune only explicitly selected unused
  artifacts. Live or unverifiable leases block pruning.

Current binary artifacts target Windows x64 and Linux x64 with glibc 2.39+.
An unsupported host fails before a download. Framework client support is
qualified separately from runtime host packaging.

The packaged runtime is currently v0.2.2. New environment-profile helpers and
some native clients require the accompanying runtime changes, which are awaiting
a runtime release. See [supported integrations](compatibility/README.md) for the
current boundaries.

## Mimic capabilities and contributor experiments

The canonical [schema](schema/mimic/protocol.json) generates models and typed
commands for profiles, media, resource policy, snapshots, traces and diagnostics.
Use integration context helpers to apply capabilities before user pages exist;
use the page-scoped extension client for page commands.

[Media factories](docs/media.md) discover private camera/microphone sources in
the context being configured, then assign independent public device labels,
logical identities and groups. Capture remains lazy and permission-controlled.

The experimental dispatcher is primarily for contributors testing a new runtime
command before a schema/package release:

```typescript
await session.mimic.experimental.newContributorCommand({ enabled: true });
await session.mimic.experimental.call("newContributorCommand", {
  enabled: true,
});
```

All eight languages provide the explicit raw call. Dynamic member syntax is an
additional convenience where the language supports it. Exact wire spelling,
omitted values, explicit null, session scope and error `code/message/data` survive
unchanged. It performs no schema discovery or automatic retries. A cancelled
call does not roll back a command already sent.

Contributor workflow explanations belong in contributor/API documentation;
do not copy the feature proposal into runtime changelogs. A real public contract
change still needs an accurate, concise release note in the owning package.

## Independent releases

The source home is the `mimic-browser/sdk` monorepo. Runtime binaries
remain in `mimic-browser/runtime`; the website remains in `mimic-browser/website`.
An SDK release intentionally selects changed, qualified native packages.
A runtime release runs compatibility qualification; it does not bump, rewrite
or publish SDK packages, defaults or user locks.

Read [architecture](docs/architecture.md), [contributing](CONTRIBUTING.md),
[binding conventions](spec/bindings.md) and the [release tooling](release/).
The existing [Prosperity license](LICENSE) is included in each distribution.
