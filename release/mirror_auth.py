"""Temporary SSH credentials for the dedicated PHP distribution repository."""
from contextlib import contextmanager
from pathlib import Path
import os
import shlex
import tempfile

import release

MIRROR = "git@github.com:mimic-browser/sdk-php.git"


@contextmanager
def credentials(environment):
    key = environment.get("PHP_MIRROR_SSH_KEY")
    if not key:
        raise release.ReleaseError("PHP mirror requires PHP_MIRROR_SSH_KEY")
    with tempfile.TemporaryDirectory(prefix="mimic-mirror-key-") as temporary:
        path = Path(temporary) / "identity"
        path.write_text(key.rstrip() + "\n", encoding="utf-8")
        path.chmod(0o600)
        known_hosts = Path(__file__).with_name("github_known_hosts").resolve()
        command = ["ssh", "-i", str(path), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts)]
        child = {**environment, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": shlex.join(command)}
        child.pop("PHP_MIRROR_SSH_KEY", None)
        yield MIRROR, child
