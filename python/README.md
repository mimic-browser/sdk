# Mimic for Python

Licensed under the included Prosperity Public License 3.0.0.

Install the SDK from PyPI and the client you want to use:

```sh
python -m pip install mimic-browser
python -m pip install playwright==1.63.0
```

Source installation is also available with
`python -m pip install "mimic-browser @ git+https://github.com/mimic-browser/sdk.git@v0.1.0#subdirectory=python"`.

Python 3.10+ is required. The first `launch()` downloads Mimic and later runs
reuse it. No executable path or `playwright install` step is needed. The native runtime
manager does not depend on Node; Playwright retains its own normal packaged
driver. Browser, context, page and locator objects are real framework objects.

```python
from mimic.playwright.sync_api import launch

with launch() as session:
    context = session.new_context()
    page = context.new_page()
    page.goto("https://example.com")
    print(page.title())
    print(session.mimic.get_version().version)
```

Save the script as `scrape.py` and run `python scrape.py`.

The async integration follows Python's async Playwright API:

```python
import asyncio
from mimic.playwright.async_api import launch

async def main():
    async with await launch() as session:
        context = await session.new_context()
        page = await context.new_page()
        await page.goto("https://example.com")
        print(await page.title())

asyncio.run(main())
```

`connect(endpoint, playwright=existing_driver)` never downloads or spawns Mimic.
Closing it preserves the external runtime and borrowed driver. Launch owns its
runtime and stops it on teardown or failed attachment. Imports are inert; Mimic
launches always use headless mode and an OS-assigned loopback port.

Context media and resource policy can be set with `new_context(media=...,
resource_policy=...)`. An owned temporary blank probe obtains the context ID
through public CDP, then closes before settings or user pages. Managed profiles
require the explicit runtime configure-context bridge. Unsupported options fail
instead of producing a misleading profile. Framework options use native
Playwright keyword names.

The bundled runtime supports
`new_context(profile={"generate": {"seed": "repeatable"}})`.
The helper sets `no_viewport=True` and uses Playwright's
explicit `"null"` media defaults so a frozen profile stays authoritative. It
rejects competing framework emulation options before creating the context.

`media` can also be a callable receiving `ContextSetup(browser_context_id, mimic)`;
the async integration also awaits async factories. Discover sources with the
explicit context ID and return a dictionary or generated `MediaConfiguration`.
Private capture sources and public labels/groups are separate fields. Factory
failure closes the newly created context. See [media configuration](../docs/media.md)
and `../examples/python/media.py`. Media methods require an explicit context ID;
`for_page` does not infer one when their optional parameter is omitted.

Stable typed methods use snake_case and generated dataclasses. Dataclass fields
also use snake_case; generated serialization retains exact wire names. `UNSET`
means omitted; `None` remains JSON null where supplied. Arbitrary raw JSON uses
dictionaries with exact wire keys.

```python
await session.mimic.experimental.newContributorCommand({"enabled": True})
await session.mimic.experimental.call("newContributorCommand", {"enabled": True})
page_mimic = await session.for_page(page)
await page_mimic.start_trace()
```

Experimental names retain exact wire spelling. Access and introspection are
inert; explicit `call` supports names colliding with Python members. Stable and
experimental calls share the same extension dispatcher, timeout, routing and
connection. The extension connection preserves protocol error code, message and
data that framework send methods can lose. Dispatched calls are never retried
automatically; cancellation cannot imply rollback.

Optional `python -m pip install 'mimic-browser[pyppeteer]'` provides the Pyppeteer
2.0.0 integration via `mimic.pyppeteer`. It returns real Pyppeteer objects,
whose automation methods retain that project's existing naming. It connects to
Mimic without invoking Chromium download or launch. Keep Pyppeteer in a separate
environment from the transport test extra, whose newer WebSocket dependency is
incompatible with Pyppeteer's dependency range.

```python
import asyncio
from mimic.pyppeteer import launch

async def main():
    async with await launch() as session:
        context = await session.new_context()
        page = await context.newPage()
        await page.goto("https://example.com")
        print(await page.title())

asyncio.run(main())
```

`RuntimeManager` supplies `install`, `inspect`, `verify`, `prune` and `launch`.
`launch(runtime_version="0.2.3")` selects an explicit release when your project
needs a pin.
`mimic-sdk install|list|verify|prune` exposes artifact operations. Preinstall an
official archive with `install(archive_path=...)`. Installation can run offline
with `allow_download=False` or `MIMIC_DOWNLOAD=0`. SDK default, explicit version,
lock file and executable selection follow `../spec/runtime-manager.md`. Pins do
not silently advance with a new runtime or SDK release. Pruning is explicit and
rejects live or unverifiable leases.

To include the optional framework in one installation, use
`python -m pip install 'mimic-browser[playwright]'`.
For local development, use `python -m pip install -e '.[playwright]'` in this
directory, or select `.[pyppeteer]`. Run `python -m unittest discover -s tests -p test_unit.py` for checks without
listeners. Run `python tests/integration.py` and `python tests/transport.py`
inside Linux/WSL for real Playwright and raw protocol qualification. Run
`python tests/pyppeteer_integration.py` in the Pyppeteer environment.
