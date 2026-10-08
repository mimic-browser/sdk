from mimic.playwright.sync_api import launch
from mimic.playwright.async_api import launch as launch_async
from mimic.pyppeteer import launch as launch_pyppeteer

def sync_client() -> None:
    session = launch(runtime_versoin="0.2.3")
    session.new_context(localle="en-US")
    session.new_context(viewport="wide")
    session.new_context(framework={"localle": "en-US"})
    value: int = session.mimic.get_version().version
    session.for_page("not a native Page")

async def async_client() -> None:
    session = await launch_async(engnie="v8")
    await session.new_context(framework={"viewport": "wide"})
    await session.for_page(17)
    await launch_pyppeteer(runtime_versoin="0.2.3")
