from typing_extensions import assert_type
from mimic.playwright.sync_api import launch, ContextSetup
from mimic.playwright.async_api import launch as launch_async
from mimic.pyppeteer import launch as launch_pyppeteer
from mimic.generated import GetVersionResult, MediaConfiguration
from playwright.sync_api import BrowserContext, Page
from playwright.async_api import BrowserContext as AsyncBrowserContext

def media(setup: ContextSetup) -> MediaConfiguration:
    version = setup.mimic.get_version()
    assert_type(version, GetVersionResult)
    return MediaConfiguration(devices=[])

def sync_client() -> None:
    with launch(runtime_version="0.2.3", allow_download=False) as session:
        context = session.new_context(locale="en-US", viewport={"width": 800, "height": 600})
        assert_type(context, BrowserContext)
        assert_type(context.new_page(), Page)
        session.new_context(framework={"locale": "en-US"}, media=media)
        handle = session.for_page(context.new_page())
        assert_type(handle.closed, bool)
        assert_type(handle.get_version(), GetVersionResult)
        handle.close()

async def async_client() -> None:
    async with await launch_async(engine="v8") as session:
        context = await session.new_context(locale="en-US")
        assert_type(context, AsyncBrowserContext)
        handle = await session.for_page(await context.new_page())
        assert_type(await handle.get_version(), GetVersionResult)
        await handle.detach()
    async with await launch_pyppeteer(runtime_version="0.2.3") as legacy:
        assert_type(await legacy.mimic.get_version(), GetVersionResult)
