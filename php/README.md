# Mimic SDK for PHP

The core package manages the real Mimic executable and provides typed extension
commands without requiring a browser automation framework. PHP 8.2+, curl, JSON,
OpenSSL, zlib and ZipArchive are required. The optional Chrome PHP adapter uses
the genuine `HeadlessChromium\Browser` and `Page` classes from
`chrome-php/chrome` (qualified with 1.16.0).

Install the SDK from Packagist with the optional Chrome PHP client:

```sh
mkdir mimic-php-example
cd mimic-php-example
composer require mimic-browser/sdk:^0.1 chrome-php/chrome:1.16.0 --no-interaction
```

No runtime path is needed: the first `launch()` downloads and verifies the
bundled runtime pin.

Save the example below as `example.php` in that project and run `php example.php`.

```php
<?php
require __DIR__ . '/vendor/autoload.php';

use Mimic\Sdk\Chrome\ChromeSession;
use Mimic\Sdk\RuntimeOptions;

$session = ChromeSession::launch();
try {
    $context = $session->newContext();
    $page = $session->newPage($context);
    $page->setHtml('<button id="run" onclick="this.textContent=\'done\'">run</button>');
    $page->dom()->querySelector('#run')->click();
    echo $page->evaluate('document.querySelector("#run").textContent')->getReturnValue();
} finally {
    $session->close();
}
```

Install `chrome-php/chrome:^1.16` only when using this adapter. It has no native
Context class, so `newContext` returns an explicit Mimic capability handle and
`newPage($context)` returns a real Chrome PHP Page. Configuration is applied
before page creation. `forPage($page)` resolves the page's actual Context.
`forPageCommands($page)` returns a cached page command handle. Its `commands`
property exposes generated methods and typed results. Call `close()` to release
that extra attachment without closing the native Page; native Page and session
closure invalidate the handle automatically.
Generated/imported profiles, proxy, media and resource policies are accepted by
`newContext`; the runtime enforces profile coherence. The bundled v0.2.3 runtime
provides the `Mimic.configureContext` bridge for managed profiles.

Use `RuntimeOptions` named arguments and generated models for editor completion:

```php
$configuration = new \Mimic\Sdk\Generated\CreateContextParams();
$configuration->media = new \Mimic\Sdk\Generated\MediaConfiguration();
$configuration->media->devices = [];
$context = $session->newContext($configuration);
```

Existing JSON arrays/objects remain accepted. Native connection settings use
Chrome PHP's ordinary options array; the adapter documents its supported keys
with array-shape annotations rather than imitating the client's API.

The optional second `newContext` argument is a media factory:
`function (ContextSetup $setup): Generated\MediaConfiguration`. It receives the
real `browserContextId` and the session's `mimic` client before any page is made.
Use the normal generated `getMediaSources` with that explicit Context ID, then
return a media configuration separating the private source from public device
identity. A factory exception disposes the newly created Context.

`ChromeSession::connect($httpOrWebSocketEndpoint)` only attaches; its `close`
disconnects and disposes Contexts made through that session, preserving other
clients. `launch` owns and terminates its child. All launches are headless and
loopback-only. Import and construction never download or launch. Explicitly
close sessions in `finally` for reliable errors and cleanup.

`RuntimeManager::install` supports exact version pins, explicit lock files,
verified shared cache, offline reuse and native binary overrides. The bundled
pin is immutable v0.2.3. `MIMIC_RUNTIME_DIR`, `MIMIC_RUNTIME_VERSION`,
`MIMIC_EXECUTABLE_PATH` and `MIMIC_DOWNLOAD=0` follow the shared specification.
Linux requires amd64/glibc 2.39; the other packaged target is Windows amd64.
The installer uses libcurl's HTTPS, proxy and CA settings. The browser Context
proxy is separate. The launcher redirects diagnostics to an owned temporary
directory (PHP's Windows pipe limitations), removing it after child exit.
`RuntimeOptions(archivePath: '/path/to/official.tar.gz', allowDownload: false)`
uses the same size/hash/extraction checks without an installer network request.

```php
$version = $session->mimic->commands->getVersion(); // Generated typed result.
$result = $session->mimic->experimental()->send('Mimic.futureCommand', [
    'unknownField' => null,
]);
```

Typed and experimental commands share one explicit raw CDP connection.
`ProtocolException` preserves native `getCode()`, `getMessage()`, arbitrary
`data`, and `hasData`. Generated optional fields default to `Missing::Value`
for omission and native `null` for explicit JSON null. Returned models hydrate
through `Model::fromWire`; arbitrary object properties remain intact.

Run offline unit checks with `composer test`. Run live qualification only on
Linux: `php tests/integration.php /absolute/path/to/mimic`; installer qualification
uses `php tests/integration.php --install /absolute/cache`. These tests exercise
the real optional client, managed profiles, Context capability setup, raw error
fidelity, attach isolation, timeout cleanup and owned process shutdown.

This package includes the repository's Prosperity Public License 3.0.0.

Media configuration or a media factory alone preserves an ordinary Context and
its native CDP emulation. An explicit `profile` or `proxy` opts into a managed
environment. A factory receives the same Context's `browserContextId` and typed
`mimic` client before user Pages exist; private capture sources and public device
labels remain separate.
