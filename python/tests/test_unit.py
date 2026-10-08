import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from mimic.protocol import Experimental, OMITTED, json_value
from mimic.runtime import RuntimeManager, RuntimeError, normalize_version, validate_lock, extract_archive


class ContractTests(unittest.TestCase):
    def test_experimental_dynamic_calls_and_inspection(self):
        calls = []
        proxy = Experimental(lambda *args: calls.append(args))
        method = proxy.newFutureCommand
        repr(proxy)
        self.assertEqual(calls, [])
        method({"key": None})
        proxy.call("newFutureCommand", {"key": None})
        self.assertEqual(calls[0], calls[1])
        proxy.call("__str__", None)
        proxy.call("call")
        self.assertIs(calls[-1][1], OMITTED)
        with self.assertRaises(ValueError):
            proxy.call("Runtime.evaluate")

    def test_lossless_json(self):
        for value in (float("nan"), float("inf"), {1: "wrong"}, {"x"}, (1, 2), object()):
            with self.assertRaises(TypeError):
                json_value(value)
        json_value({"x": [None, False, 3, "value"]})

    def test_manifest_and_selectors(self):
        import mimic.runtime
        lock = json.loads(Path(mimic.runtime.__file__).with_name("runtime-lock.json").read_text())
        validate_lock(lock)
        self.assertEqual(normalize_version("0.2.2"), "v0.2.2")
        for value in ("latest", "^0.2", "../../0.2.2", "0.2"):
            with self.assertRaises(RuntimeError):
                normalize_version(value)
        with self.assertRaises(RuntimeError):
            RuntimeManager(lock=lock, runtime_version="999.0.0")
        lock["manifestJson"] += " "
        with self.assertRaises(RuntimeError):
            validate_lock(lock)

    def test_archive_traversal_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("root/../../escape", "/root/absolute", "root\\escape", "C:/escape"):
                archive = root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr(name, b"bad")
                with self.assertRaises(RuntimeError):
                    extract_archive(archive, root / "out", "root")
            archive = root / "link.zip"
            with zipfile.ZipFile(archive, "w") as output:
                info = zipfile.ZipInfo("root/link")
                info.external_attr = 0o120777 << 16
                output.writestr(info, "../outside")
            with self.assertRaises(RuntimeError):
                extract_archive(archive, root / "out", "root")

    def test_explicit_executable_obeys_lock_hash(self):
        import mimic.runtime
        lock = json.loads(Path(mimic.runtime.__file__).with_name("runtime-lock.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "mimic"
            executable.write_text("never execute this unverified file")
            with self.assertRaisesRegex(RuntimeError, "SHA256"):
                RuntimeManager(executable_path=executable, lock=lock).install()
            self.assertEqual(RuntimeManager(executable_path=executable, allow_download=False).install(), executable)

    def test_downloaded_manifest_preserves_crlf_hash(self):
        import mimic.runtime
        default = json.loads(Path(mimic.runtime.__file__).with_name("runtime-lock.json").read_text())
        manifest = default["manifest"]
        original_version = manifest["version"]
        manifest["version"] = "v999.0.0"
        for artifact in manifest["artifacts"]:
            artifact["binaryVersion"] = "v999.0.0"
            artifact["archive"] = artifact["archive"].replace(original_version, "v999.0.0")
        raw = json.dumps(manifest, indent=2).replace("\n", "\r\n") + "\r\n"
        with tempfile.TemporaryDirectory() as temporary:
            manager = RuntimeManager(runtime_version="v999.0.0", runtime_dir=temporary)
            def download(url, destination):
                content = raw if url.endswith("release-manifest.json") else hashlib.sha256(raw.encode()).hexdigest() + "  release-manifest.json\r\n"
                destination.write_bytes(content.encode())
            manager._download = download
            self.assertEqual(manager.resolve_lock()["manifestJson"], raw)


if __name__ == "__main__":
    unittest.main()
