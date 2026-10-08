"""Private, pinned editor analyzers; never runtime package dependencies."""
from pathlib import Path

import release

PHPSTAN_VERSION = '2.3.0'
YARD_VERSION = '0.9.37'


def phpstan(root):
    directory = Path(root) / ('phpstan-' + PHPSTAN_VERSION)
    directory.mkdir(parents=True, exist_ok=True)
    release.execute(['composer', 'require', '--working-dir=' + str(directory), '--dev',
                     'phpstan/phpstan:' + PHPSTAN_VERSION, '--no-interaction', '--no-progress'])
    return directory / 'vendor/bin/phpstan'


def yard(root):
    directory = Path(root) / ('yard-' + YARD_VERSION)
    directory.mkdir(parents=True, exist_ok=True)
    release.execute(['gem', 'install', 'yard', '--version', YARD_VERSION,
                     '--install-dir', directory, '--no-document'])
    return directory / ('gems/yard-' + YARD_VERSION + '/lib')
