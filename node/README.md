# Mimic for Node.js and TypeScript

Install the SDK from npm:

```sh
npm install mimic-browser
```

Requires Node.js 22.19 or newer. Licensed under the included Prosperity Public
License 3.0.0. The first
`launch()` downloads Mimic; later runs reuse the verified runtime. There is no
separate runtime path or Chromium installation to configure.
Import the integration you use; browser, context, page and locator objects are
the original framework objects.

Playwright and Puppeteer are optional. Keep the client already in your project,
or install the one you choose before importing its adapter:

```sh
npm install playwright-core@1.63.0
```

For Puppeteer, use `npm install puppeteer-core@25.10.0` instead and import from
`mimic-browser/puppeteer`. Installing or importing the core SDK does not install
either framework.

Source installation is also available with
`npm install git+https://github.com/mimic-browser/sdk.git#v0.1.0`.
Git installation builds the package automatically; runtime acquisition still
happens only on `launch()`.

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

Save the script as `scrape.mjs` and run `node scrape.mjs`.

Use `mimic-browser/puppeteer` for Puppeteer. The selected framework package
is an optional peer dependency: Playwright Core 1.63.0 or Puppeteer Core 25.10.0.
For that integration, install `puppeteer-core@25.10.0` instead of Playwright Core.
The SDK never downloads Chromium and always starts Mimic headless on an
OS-assigned loopback port. Package import performs no I/O or process startup.

`connect("http://127.0.0.1:9222")` attaches without installation or process
creation. Closing an attached session disconnects and closes its helper-created
contexts; it does not terminate the external runtime. `launch` owns its process
and cleans it up after failed client attachment, cancellation or session close.

Use `session.newContext({media: {devices: []}, resourcePolicy: {...}})` to apply
Context settings before user pages exist. The helper briefly opens and closes an
owned blank probe to obtain the context ID through public CDP APIs. Framework
options go under `framework`; no private client fields are read or patched.
Managed environment profiles require the runtime's explicit configure-context
bridge; older releases fail clearly instead of pretending ordinary contexts have
the selected profile.

The bundled runtime supports `newContext({profile: {generate:
{seed: "repeatable"}}})`. The helper disables framework viewport/media defaults and rejects
explicit competing emulation settings; it never changes a profile after a user
Page exists.

`media` also accepts an async factory receiving `{browserContextId, mimic}`.
Discover private sources with `mimic.getMediaSources({browserContextId})`, then
return a media configuration with separate `source` and public device labels.
The factory runs before configuration and user pages; failure closes its context.
See [private capture and public device identity](../docs/media.md) and the full
camera/microphone example in `../examples/node/media.mjs`. Media commands require
an explicit context ID; `forPage` does not implicitly scope their optional ID.

Stable typed methods and `session.mimic.experimental` share one raw extension
connection. The separate extension transport preserves error code, message and
data that framework `CDPSession.send` APIs may discard. It is owned by the
integration; experimental calls do not create another connection or discover
commands.

```typescript
await session.mimic.experimental.newContributorCommand({ enabled: true });
await session.mimic.experimental.call("newContributorCommand", {
  enabled: true,
});
const pageMimic = await session.forPage(page);
await pageMimic.startTrace();
await pageMimic.detach(); // Releases only this extension attachment.
```

Repeated `forPage(page)` calls share one active handle. Native `page.close()`
automatically releases it; explicit `close()` and `detach()` are idempotent and
leave the Page open. A released handle reports `closed: true` and rejects new
stable and experimental calls. Calling `forPage` again on an open Page creates
a new attachment. Session teardown waits for in-progress attachment and detach
operations before closing the shared transport.

TypeScript retains each framework's native context and connection option types.
For example, Playwright `newContext({framework: {locale: "en-US"}})` offers the
same native option names and value types as `browser.newContext`.

Unknown dynamic names retain their exact spelling. `then`, symbols, inspection
and serialization are inert; reserved names remain available through `call`.
Omission differs from explicit null. Unsupported JSON values fail before send.
`ProtocolError` exposes `code`, `message` and `data`. A timeout/cancellation cannot
undo a command already dispatched; the SDK does not retry state-changing calls.

`RuntimeManager` provides `install`, `inspect`, `verify`, `prune` and `launch`.
An explicit `launch({runtimeVersion: "0.2.3"})` selects a runtime release when
your project needs a pin.
Use `install({archivePath})` to preinstall a separately obtained verified archive.
`allowDownload:false` or `MIMIC_DOWNLOAD=0` makes installation strictly offline.
The packaged default remains exactly v0.2.3; runtime releases do not update SDK
packages or change saved pins. The common cache, lock, receipt, requirements and
selector rules are specified in `../spec/runtime-manager.md`. Pruning requires
an explicit call and rejects live or unverifiable leases.
An explicitly supplied lock validates even an explicit executable's SHA256
before launch. Downloads honor standard HTTP(S) proxy environment variables and
Node's configured certificate authorities without changing global dispatchers.

For local development, run `npm install --ignore-scripts && npm run build` in
this directory. `npm test` uses no listeners. Run `npm run test:integration` and
`node --test test/transport.mjs` inside Linux/WSL only. These exercise actual
framework clients, cross-language installation, corruption, ownership and raw
protocol behavior. No headful browser or firewall changes are needed.
