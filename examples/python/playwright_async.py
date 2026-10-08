import asyncio
from mimic.playwright.async_api import launch


async def main():
    async with await launch(runtime_version="0.2.2") as session:
        context = await session.new_context()
        page = await context.new_page()
        await page.goto("https://example.com")
        print(await page.title())


asyncio.run(main())
