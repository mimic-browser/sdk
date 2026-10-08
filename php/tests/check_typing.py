"""Check installed PHP editor contracts without loading or launching a browser."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--php', default='php')
    parser.add_argument('--phpstan', required=True)
    parser.add_argument('--autoload', type=Path, required=True)
    parser.add_argument('--package-root', type=Path)
    args = parser.parse_args()
    tests = Path(__file__).resolve().parent
    if args.package_root:
        installed = args.package_root.resolve()
        if installed == tests.parent.resolve():
            raise SystemExit('Packed typing check must not use the SDK source package')
        probe = ('require $argv[1]; echo (new ReflectionClass(' +
                 "'Mimic\\\\Sdk\\\\Generated\\\\MediaSource'" + '))->getFileName();')
        located = subprocess.check_output([args.php, '-r', probe, str(args.autoload.resolve())], text=True)
        if Path(located).resolve() != installed / 'src/Generated.php':
            raise SystemExit('Autoload resolved a different PHP package: ' + located)
    command = [args.php, args.phpstan, 'analyse', '--no-progress', '--level=9',
               '--error-format=json', '--autoload-file=' + str(args.autoload.resolve())]
    positive = subprocess.run([*command, str(tests / 'editor_types.php')], capture_output=True, text=True)
    if positive.returncode:
        raise SystemExit(positive.stdout + positive.stderr)
    negative = subprocess.run([*command, str(tests / 'editor_types_invalid.php')], capture_output=True, text=True)
    report = json.loads(negative.stdout)
    identifiers = {message['identifier'] for file in report['files'].values() for message in file['messages']}
    if negative.returncode == 0 or not {'property.notFound', 'argument.type', 'argument.unknown'} <= identifiers:
        raise SystemExit('Expected typo/type diagnostics were not found: ' + negative.stdout + negative.stderr)
    print('PASS: PHP typed collection/native consumers; misspelled members and wrong argument types rejected')


if __name__ == '__main__':
    main()
