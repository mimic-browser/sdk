import os
from pathlib import Path
import shlex
import unittest

import mirror_auth
import publish
import release


class MirrorAuthTests(unittest.TestCase):
    def test_key_is_private_temporary_and_removed_from_child_environment(self):
        source = {"PHP_MIRROR_SSH_KEY": "private-key", "KEEP": "value"}
        with mirror_auth.credentials(source) as (mirror, environment):
            self.assertEqual(mirror, "git@github.com:mimic-browser/sdk-php.git")
            command = shlex.split(environment["GIT_SSH_COMMAND"])
            path = Path(command[command.index("-i") + 1])
            self.assertEqual(path.read_text(), "private-key\n")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("BatchMode=yes", command)
            self.assertNotIn("PHP_MIRROR_SSH_KEY", environment)
            self.assertEqual(environment["KEEP"], "value")
        self.assertFalse(path.exists())
        self.assertEqual(source["PHP_MIRROR_SSH_KEY"], "private-key")

    def test_cleanup_on_failure_and_missing_key_preflight(self):
        with self.assertRaisesRegex(ValueError, "failure"):
            with mirror_auth.credentials({"PHP_MIRROR_SSH_KEY": "private-key"}) as (_, environment):
                path = Path(shlex.split(environment["GIT_SSH_COMMAND"])[2])
                raise ValueError("failure")
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(release.ReleaseError, "PHP_MIRROR_SSH_KEY"):
            publish.check_credentials({"php": {"registry": "packagist"}}, {})
        publish.check_credentials({"php": {"registry": "packagist"}}, {"PHP_MIRROR_SSH_KEY": "private-key"})
