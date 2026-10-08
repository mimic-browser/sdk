from mimic.playwright.sync_api import launch

with launch(runtime_version="0.2.2") as session:
    page = session.new_context().new_page()
    page.goto("https://example.com")
    print(page.title())
    print(session.mimic.get_version().version)
