"""Qualify packed SDKs on a GitHub-hosted Windows runner without Chromium."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    if (os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('RUNNER_OS') != 'Windows'):
        raise RuntimeError('This qualification runs only on GitHub-hosted Windows; never on a local workstation')
    root = Path(__file__).resolve().parents[2]
    output = root / '.build/windows-qualification'
    archives, wheels = list(output.glob('mimic-browser-*.tgz')), list(output.glob('mimic_browser-*.whl'))
    if len(archives) != 1 or len(wheels) != 1:
        raise RuntimeError('Expected exactly one freshly packed Node archive and Python wheel')
    lock = json.loads((root / 'release/runtime-lock.json').read_text())
    if lock['release'] != os.environ['MIMIC_EXPECTED_RELEASE']:
        raise RuntimeError('Bundled runtime does not match the requested qualification release')
    node = Path(shutil.which('node')).resolve()
    npm = node.parent / 'node_modules/npm/bin/npm-cli.js'
    if not npm.is_file():
        raise RuntimeError('Cannot find setup-node npm CLI')
    record = {'passed': False, 'runtimeRelease': lock['release'], 'manifestSha256': lock['manifestSha256'],
              'packages': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in archives + wheels},
              'checks': []}
    environment = dict(os.environ)
    for key in ('MIMIC_RUNTIME_DIR', 'MIMIC_RUNTIME_VERSION', 'MIMIC_EXECUTABLE_PATH', 'MIMIC_DOWNLOAD', 'PYTHONPATH', 'NODE_PATH'):
        environment.pop(key, None)
    environment.update(PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1', PUPPETEER_SKIP_DOWNLOAD='1')

    def run(args, cwd, env=environment, timeout=180):
        result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'Command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}')
        return result.stdout.strip()

    try:
        with tempfile.TemporaryDirectory(prefix='Mimic SDK Windows ', dir=os.environ['RUNNER_TEMP']) as temporary:
            work = Path(temporary).resolve()
            if not work.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve()):
                raise RuntimeError('Temporary consumer directory escaped runner storage')
            consumer = work / 'node consumer'
            consumer.mkdir()
            (consumer / 'package.json').write_text('{"private":true,"type":"module"}\n')
            shutil.copyfile(root / 'conformance/windows/node.mjs', consumer / 'qualification.mjs')
            shutil.copyfile(root / 'conformance/windows/python.py', work / 'qualification.py')
            run([node, npm, 'install', '--no-audit', '--no-fund', archives[0]], consumer)
            interpreters = {}
            for name, requirements in [('core', []), ('playwright', ['playwright==1.63.0']), ('pyppeteer', ['pyppeteer==2.0.0'])]:
                directory = work / name
                run([sys.executable, '-m', 'venv', directory], work)
                python = directory / 'Scripts/python.exe'
                run([python, '-m', 'pip', 'install', wheels[0], *requirements], work)
                interpreters[name] = python

            def check(client, mode, cache, offline=False):
                env = {**environment, 'LOCALAPPDATA': str(work / cache)}
                if offline:
                    env['MIMIC_DOWNLOAD'] = '0'
                args = ([node, consumer / 'qualification.mjs', mode] if client == 'node'
                        else [interpreters[client], '-I', work / 'qualification.py', mode])
                return json.loads(run(args, consumer if client == 'node' else work, env))

            record['checks'].append({'client': 'node', **check('node', 'core', 'core Node cache')})
            record['checks'].append({'client': 'python', **check('core', 'core', 'core Python cache')})
            shutil.copyfile(root / 'python/tests/test_unit.py', work / 'test_unit.py')
            run([interpreters['core'], '-I', work / 'test_unit.py'], work)
            record['checks'].append({'client': 'python', 'mode': 'installed-unit', 'passed': True})
            run([node, npm, 'install', '--no-audit', '--no-fund', 'playwright-core@1.63.0', 'puppeteer-core@25.10.0'], consumer)
            for client, mode, cache in [('node', 'live', 'Node cold cache'), ('playwright', 'live', 'Python cold cache'), ('pyppeteer', 'pyppeteer', 'Pyppeteer cold cache')]:
                record['checks'].append({'client': client, **check(client, mode, cache)})
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = [pool.submit(check, client, 'install', 'Shared concurrent cache') for client in ('node', 'core')]
                installs = [task.result() for task in tasks]
            if installs[0] != installs[1]:
                raise AssertionError('Node and Python did not publish the identical shared installation')
            for client in ('node', 'core'):
                if check(client, 'install', 'Shared concurrent cache', offline=True) != installs[0]:
                    raise AssertionError('Offline shared-cache reuse changed provenance')
            record['checks'].append({'mode': 'cross-language', 'coldConcurrentInstall': True, 'offlineReuse': True, 'receipt': installs[0]['receipt']})
            record['passed'] = True
    except BaseException as error:
        record['error'] = str(error)
        raise
    finally:
        (output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
