# Mimic for .NET

Requires .NET 8 or newer. The core `mimic-browser` package has no automation-framework
dependency. Select `mimic-browser.Playwright` or `mimic-browser.PuppeteerSharp` separately.
Both return the original framework interfaces, with their original exceptions,
events, options, and automation methods.

Package IDs describe installation; C# namespaces remain `Mimic.Sdk`,
`Mimic.Playwright` and `Mimic.PuppeteerSharp`.

Install from the GitHub source repository. NuGet packages are not published yet;
the example builds the actual SDK projects through native project references:

```sh
git clone https://github.com/mimic-browser/sdk.git
cd sdk
dotnet run --project examples/dotnet/Example.csproj
```

For a new project inside that checkout, use a source reference; NuGet
publication is not required:

```sh
dotnet new console --framework net8.0 --name MimicExample
dotnet add MimicExample/MimicExample.csproj reference dotnet/Mimic.Playwright/Mimic.Playwright.csproj
```

Put the following in `MimicExample/Program.cs`, then run
`dotnet run --project MimicExample`. The SDK installs Mimic on first launch.

```csharp
using Mimic.Playwright;
using Mimic.Sdk;

await using var session = await PlaywrightSession.LaunchAsync();
var context = await session.NewContextAsync();
var page = await context.NewPageAsync();
await page.GotoAsync("https://example.com");
Console.WriteLine(await page.TitleAsync());
var version = await session.Mimic.Commands.GetVersionAsync();
```

The first launch installs the pinned, verified runtime into the shared OS cache.
`AllowDownload = false` requires a complete verified cache. `ExecutablePath` uses
an explicit binary and does not install. `Version`, `LockFile`, environment
selectors and platform constraints follow `spec/runtime-manager.md`.

`RuntimeManager.InstallAsync`, `ResolveLockAsync`, `List`, `VerifyAsync`, and
deliberate `Prune` expose the install lifecycle. Pruning refuses active or
unrepaired leases. Installation and launch lease creation share the same lock.
An existing unknown installation lock times out rather than being removed.

`PlaywrightSession.ConnectAsync(endpoint)` and `PuppeteerSession.ConnectAsync`
never install or spawn. Disposing a connected session disconnects its client and
closes contexts created through its helper; the server and unrelated clients
remain available. A borrowed `IPlaywright` driver is never disposed by the SDK.
Owned launches stop their own Mimic process. Disposal is idempotent.

`session.ForContextAsync(context)` exposes Mimic capabilities for the same native
Playwright Context. It discovers its CDP ID using a public CDP session and removes
its temporary blank page before returning. PuppeteerSharp exposes the Context ID
directly. `NewContextAsync` accepts `media` and `resourcePolicy` configuration before
the application creates its first page. No private framework fields are accessed.

`NewConfiguredContextAsync(configuration)` also supports coherent generated or
imported profiles and proxy settings through `Mimic.configureContext` on the
bundled v0.2.3 runtime. Its Playwright defaults use the public no-viewport and null
media sentinels so framework defaults do not alter a managed profile. Conflicting
identity, geometry or media options are rejected before a Context is created.
PuppeteerSharp sessions default to `DefaultViewport = null`; callers using
managed profiles must retain that setting.

Both helpers accept an asynchronous `mediaFactory` returning a generated
`MediaConfiguration`. Its `ContextSetup` provides the actual `BrowserContextId`
and the same `Mimic` client; call `setup.Mimic.Commands.GetMediaSourcesAsync(new()
{ BrowserContextId = Optional<string>.Of(setup.BrowserContextId) })` to inspect
Context-bound private sources before selecting them. Source choice and public
device identity stay separate. The helper closes its probe before the callback;
callback failure closes the new Context. Direct media objects remain supported.

`RuntimeOptions.ArchivePath` installs a locally supplied official archive through
the same manifest size/hash/extraction checks, including with downloads disabled.

All generated models and `MimicCommands` are in `Mimic.Sdk.Generated`.
Use `Optional<T>.Of(value)` for an explicitly present field and
`Optional<T>.Null()` for explicit JSON null; the default value means omission.
Use `Wire.Encode` and `Wire.Decode` for serialization.

`session.Mimic.Experimental.SendAsync("Mimic.futureMethod", json)` sends the exact
method and JSON over the same raw transport as typed commands. It does not probe
or retry calls. `ProtocolException.Code`, `Message`, and `DataValue` retain the
runtime's protocol error. Page/session scope is explicit through
`ProtocolConnection.Session(sessionId)`; browser-scope calls are not silently
redirected to a current page.

Qualification commands:

```sh
dotnet run --project dotnet/Mimic.Tests
# Linux only; starts headless, loopback-only Mimic instances:
dotnet run --project dotnet/Mimic.Tests -- --integration /absolute/path/to/mimic
dotnet run --project dotnet/Mimic.Tests -- --integration-candidate /absolute/path/to/mimic
dotnet run --project dotnet/Mimic.Tests -- --install /tmp/mimic-sdk-cache
```

The tests include shared generated wire cases, integrity, exact pins, offline
selection, archive traversal, real framework objects, context setup, errors,
attach isolation, process cleanup, cache receipts, and lease protection. Runtime
browser limitations remain visible: for example, top-level `data:` navigation
is unsupported; use HTTP(S), `about:blank`, or the framework's content setter.
Client qualification is recorded separately from successful package compilation.
