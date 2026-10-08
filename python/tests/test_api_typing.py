"""Check generated native keyword names against the qualified framework."""
import importlib.util
from pathlib import Path
import unittest


class NativeTypingTests(unittest.TestCase):
    def test_generated_playwright_signatures_are_current(self):
        if importlib.util.find_spec("playwright") is None:
            self.skipTest("Install the qualified Playwright extra to check native signatures")
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("api_typing", root / "tools/generate_api_typing.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        for path, expected in generator.render().items():
            self.assertEqual((root / path).read_text(), expected, path)
