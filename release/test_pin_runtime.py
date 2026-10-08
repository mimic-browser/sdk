import json
from pathlib import Path
import unittest

import pin_runtime
from mimic.runtime import RuntimeError as MimicRuntimeError


class OfficialPinTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        self.manifest = (root / "runtime-manifest.json").read_bytes()
        self.sums = (root / "runtime-SHA256SUMS").read_bytes()
        self.version = json.loads(self.manifest)["version"]

    def test_retains_exact_published_bytes(self):
        lock = pin_runtime.verified_lock(self.version, self.manifest, self.sums)
        self.assertEqual(lock["manifestJson"].encode(), self.manifest)

    def test_all_distributions_share_the_default_pin(self):
        expected = json.loads((pin_runtime.ROOT / pin_runtime.COPIES[0]).read_text(encoding="utf-8"))
        for name in pin_runtime.COPIES:
            self.assertEqual(json.loads((pin_runtime.ROOT / name).read_text(encoding="utf-8")), expected, name)

    def test_rejects_changed_manifest_and_wrong_release(self):
        with self.assertRaises(ValueError):
            pin_runtime.verified_lock(self.version, self.manifest + b" ", self.sums)
        with self.assertRaises(MimicRuntimeError):
            pin_runtime.verified_lock("99.0.0", self.manifest, self.sums)

    def test_rejects_archive_checksum_disagreement(self):
        archive = json.loads(self.manifest)["artifacts"][0]
        changed = self.sums.replace(archive["sha256"].encode(), b"0" * 64)
        with self.assertRaises(ValueError):
            pin_runtime.verified_lock(self.version, self.manifest, changed)


if __name__ == "__main__":
    unittest.main()
