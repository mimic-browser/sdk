# Mimic for Java and Kotlin

Requires Java 17 or newer. `boo.mimic:mimic-browser` contains the native runtime
manager and typed extension API. Playwright Java is an optional Maven dependency;
add `com.microsoft.playwright:playwright:1.63.0` when importing the
`io.mimicbrowser.sdk.playwright` adapter. No Chromium download or framework launcher
is involved. Kotlin uses the same Java artifact.

Maven coordinates and Java package names serve different purposes: imports
remain under `io.mimicbrowser.sdk`.

Install from the GitHub source repository. Maven Central packages are not
published yet. Maven installs the SDK into your local artifact cache, so the
included Kotlin consumer uses ordinary Maven dependency coordinates:

```sh
git clone https://github.com/mimic-browser/sdk.git
cd sdk
mvn -B -ntp -f java/pom.xml install -Dmaven.test.skip=true
mvn -f examples/java/kotlin/pom.xml compile exec:java
```

The consumer uses the ordinary bundled runtime pin; first launch downloads and
verifies Mimic automatically. Add the locally installed SDK and optional
Playwright dependency to another project's POM using the coordinates above.

```java
try (var session = PlaywrightSession.launch(new RuntimeOptions())) {
    var context = session.newContext();
    var page = context.newPage();
    page.navigate("https://example.com");
    System.out.println(page.title());
    var version = session.mimic().commands().getVersion(new Generated.GetVersionParams());
}
```

The first launch installs the exact verified runtime. Cache, manifests, locks,
receipts and leases are shared with other SDK languages. `allowDownload(false)`
uses the verified local cache without networking. An explicit executable does not
download and preserves a truthful development identity; an explicit version or
lock additionally constrains that binary. The full contract is
`spec/runtime-manager.md`.

`RuntimeManager` provides `install`, `launch`, `resolveLock`, `list`, `verify`, and
deliberate `prune`. Pruning refuses all live or unrepaired leases. Installation,
startup lease creation and pruning use the same cross-language directory lock.
Unknown locks are not removed solely because of age.

`PlaywrightSession.connect(endpoint)` never installs or spawns. `close()` releases
its own connection and helper-created contexts without stopping the remote
server. `launch` owns its runtime process. An optionally supplied Playwright
driver remains owned by its caller. Keep Playwright use on its owning thread,
following the native client's threading contract.

`forContext` discovers a native Context ID through public CDP APIs. A temporary
blank probe is closed before returning. `newContext(options, media, policy)`
configures the actual Context before application pages are created. Native
`Browser`, `BrowserContext`, `Page`, and `Locator` types remain intact.

`newConfiguredContext(configuration, options)` applies coherent generated or
imported profiles and proxy settings via `Mimic.configureContext` in the bundled
v0.2.3 runtime. Public Playwright Optional-null settings disable its default
viewport and media emulation for managed profiles. Conflicting identity, geometry
or media options are rejected before Context creation. Supplied options are copied.

The overload with a `Function<ContextSetup, Generated.MediaConfiguration>` media
factory runs after the blank probe is closed. `setup.browserContextId()` is
explicit input to `setup.mimic().commands().getMediaSources(params)`; source
selection and public device identity remain distinct. Direct media objects still
work. A failed factory closes the Context before propagating the original error.
`RuntimeOptions.archivePath(path)` supports fully verified offline installation
from an official local archive.

Generated models are nested in `Generated`. A null `OptionalValue<T>` field means
omitted; `OptionalValue.of(null)` means explicit JSON null. Serialize with
`Generated.toWire` / `Generated.fromWire`, which preserve these distinctions.

`session.mimic().experimental().send("Mimic.futureMethod", json)` shares the typed
commands' raw connection, without schema membership checks or retries.
`ProtocolException` retains `code()`, `getMessage()`, and arbitrary JSON `data()`.
Use `ProtocolConnection.session(sessionId)` for explicitly scoped page commands;
the SDK never guesses a current page. Blocking transport/installer operations
honor thread interruption and bounded timeouts.

Run `mvn test` for non-listening unit checks. On Linux, run
`IntegrationCheck` from the test classpath with an explicit Mimic executable, or
`--install /tmp/mimic-sdk-cache` for the shared installer. See
`examples/java/kotlin` for a compiled Kotlin consumer. Browser capabilities remain
bounded by the selected Mimic runtime. Top-level `data:` navigation is unsupported;
use HTTP(S), `about:blank`, or the framework's content setter.
