from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from model import ROOT, ContractError, load
import generate


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load()
        cls.fixtures = json.loads((ROOT / "conformance/fixtures/wire.json").read_text())["cases"]
        spec = importlib.util.spec_from_file_location("generated_conformance", ROOT / "python/mimic/generated.py")
        cls.python = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.python
        spec.loader.exec_module(cls.python)

    def test_shared_schema_corpus(self):
        for case in self.fixtures:
            with self.subTest(case=case["name"]):
                schema = self.contract.definitions[case["model"]]
                if case["valid"]:
                    self.contract.validate(schema, case["wire"])
                else:
                    with self.assertRaises(ContractError):
                        self.contract.validate(schema, case["wire"])

    def test_python_roundtrip_shared_valid_corpus(self):
        for case in self.fixtures:
            if not case["valid"]:
                continue
            with self.subTest(case=case["name"]):
                value = self.python.from_wire(getattr(self.python, case["model"]), case["wire"])
                self.assertEqual(case["wire"], self.python.to_wire(value))

    def test_python_explicit_null_is_retained_for_validation(self):
        value = self.python.CreateContextParams(dispose_on_detach=None)
        self.assertEqual({"disposeOnDetach": None}, self.python.to_wire(value))
        self.assertEqual({}, self.python.to_wire(self.python.CreateContextParams()))

    def test_stable_unknown_method_is_not_discovered(self):
        with self.assertRaises(ContractError):
            self.contract.command("Mimic.someFutureExperiment")

    def test_generated_outputs_match_exact_bytes(self):
        for filename, backend in (generate.OUTPUTS | generate.COPIES).items():
            with self.subTest(target=filename):
                self.assertEqual((ROOT / filename).read_bytes(), backend().encode())

    def test_source_provenance_and_frozen_chrome(self):
        provenance = json.loads((ROOT / "schema/mimic/source.json").read_text())
        self.assertEqual(self.contract.sha256, provenance["contractSha256"])
        chrome = ROOT / "schema/sources/chrome152/chrome152.json"
        source = json.loads(chrome.with_suffix(".source.json").read_text())
        self.assertEqual(hashlib.sha256(chrome.read_bytes()).hexdigest(), source["sha256"])
        self.assertEqual("152.0.7977.82", source["captureMetadata"]["chromeVersion"])

    def test_closed_input_and_no_invented_events(self):
        self.assertEqual([], self.contract.source["events"])
        for command in self.contract.commands:
            params = self.contract.resolve(command["params"])
            self.assertIs(params["additionalProperties"], False)
            self.assertIn(command["scope"], ("browser", "context", "session"))


if __name__ == "__main__":
    unittest.main()
