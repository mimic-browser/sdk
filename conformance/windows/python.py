"""Installed-consumer checks; execution is restricted to hosted Windows CI."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys

from mimic import RuntimeManager

assert os.name == 'nt'
assert os.environ.get('GITHUB_ACTIONS') == 'true'
assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
assert os.environ.get('RUNNER_OS') == 'Windows'
mode = sys.argv[1]
manager = RuntimeManager()
cache = Path(os.environ['LOCALAPPDATA']) / 'Mimic/runtimes'
assert manager.root == cache
lock = manager.resolve_lock()
assert lock['release'] == os.environ['MIMIC_EXPECTED_RELEASE']
assert next(item for item in lock['manifest']['artifacts'] if item['platform'] == 'windows-amd64')['archive'].endswith('.zip')


def verify_closed(runtime):
    assert runtime.process.poll() is not None
    runtime.close()
    binary = RuntimeManager(allow_download=False).verify()
    assert list((binary.parent / '.leases').glob('*.json')) == []


def check_runtime(session):
    assert session.runtime.identity['version'] == lock['release']
    assert session.runtime.process.args[-6:] == ['--browser-mode', 'headless', '--listen', '127.0.0.1:0', '--engine', 'v8']
    try:
        RuntimeManager().prune()
    except Exception as error:
        assert 'live' in str(error)
    else:
        raise AssertionError('Live runtime was pruned')


def sync_flow():
    from mimic.playwright.sync_api import launch, connect
    with launch() as session:
        check_runtime(session)
        runtime = session.runtime
        page = session.new_context().new_page()
        page.set_content('<title>Python Windows SDK</title><input>')
        page.locator('input').fill('native sync')
        assert page.title() == 'Python Windows SDK'
        assert page.locator('input').input_value() == 'native sync'
    verify_closed(runtime)
    os.environ['MIMIC_DOWNLOAD'] = '0'
    with RuntimeManager().launch() as external:
        retained = external.connection.call('Target.createBrowserContext', {})
        with connect(external.endpoint) as attached:
            page = attached.new_context().new_page()
            page.set_content('<title>Attached sync</title>')
            assert page.title() == 'Attached sync'
        assert external.connection.call('Mimic.getVersion')['version'] == lock['release']
        assert external.connection.call('Target.getBrowserContexts')['browserContextIds'] == [retained['browserContextId']]
    verify_closed(external)


async def async_flow(pyppeteer=False):
    if pyppeteer:
        from mimic.pyppeteer import launch, connect
    else:
        from mimic.playwright.async_api import launch, connect
    async with await launch() as session:
        check_runtime(session)
        runtime = session.runtime
        context = await session.new_context()
        page = await context.newPage() if pyppeteer else await context.new_page()
        if pyppeteer:
            await page.setContent('<title>Python Windows SDK</title><input>')
            await page.type('input', 'native async')
            assert await page.evaluate('document.querySelector("input").value') == 'native async'
        else:
            await page.set_content('<title>Python Windows SDK</title><input>')
            await page.locator('input').fill('native async')
            assert await page.locator('input').input_value() == 'native async'
        assert await page.title() == 'Python Windows SDK'
    verify_closed(runtime)
    os.environ['MIMIC_DOWNLOAD'] = '0'
    with RuntimeManager().launch() as external:
        retained = external.connection.call('Target.createBrowserContext', {})
        async with await connect(external.endpoint) as attached:
            context = await attached.new_context()
            page = await context.newPage() if pyppeteer else await context.new_page()
            if pyppeteer:
                await page.setContent('<title>Attached async</title>')
            else:
                await page.set_content('<title>Attached async</title>')
            assert await page.title() == 'Attached async'
        assert external.connection.call('Mimic.getVersion')['version'] == lock['release']
        assert external.connection.call('Target.getBrowserContexts')['browserContextIds'] == [retained['browserContextId']]
    verify_closed(external)


if mode == 'core':
    assert importlib.util.find_spec('playwright') is None
    assert importlib.util.find_spec('pyppeteer') is None
    assert not cache.exists()
    print(json.dumps({'mode': mode, 'inert': True}))
elif mode == 'install':
    binary = manager.install()
    assert binary == manager.verify()
    assert len(manager.inspect()) == 1
    print(json.dumps({'mode': mode, 'binary': str(binary), 'receipt': json.loads((binary.parent / 'installation.json').read_text())}))
elif mode in ('live', 'pyppeteer'):
    assert not cache.exists()
    if mode == 'live':
        sync_flow()
        asyncio.run(async_flow())
    else:
        asyncio.run(async_flow(pyppeteer=True))
    offline = RuntimeManager(allow_download=False)
    assert offline.install() == offline.verify()
    offline.prune()
    try:
        offline.install()
    except Exception as error:
        assert 'disabled' in str(error).lower() or 'offline' in str(error).lower()
    else:
        raise AssertionError('Offline install unexpectedly succeeded after prune')
    print(json.dumps({'mode': mode, 'release': lock['release'], 'coldDownload': True, 'offlineReuse': True, 'nativePage': True, 'attachPreserved': True, 'ownedExit': True, 'leasesCleared': True, 'prune': True}))
else:
    raise ValueError('Unknown qualification mode')
