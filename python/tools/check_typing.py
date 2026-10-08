"""Check an installed wheel with mypy, away from the source checkout.

The selected interpreter needs the wheel, mypy, and the qualified framework
extras. This check never launches a browser or downloads a runtime.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Interpreter with the installed wheel and mypy")
    parser.add_argument("--allow-source", action="store_true", help="Source CI only; qualification always checks the installed wheel")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mimic-wheel-typing-") as temporary:
        directory = Path(temporary)
        installed = subprocess.check_output([args.python, "-c", "import json,mimic; print(json.dumps(mimic.__file__))"], cwd=directory, text=True)
        package = Path(json.loads(installed)).resolve().parent
        if package.is_relative_to(root) and not args.allow_source:
            raise SystemExit("Check an installed wheel, not the source checkout")
        for expected in ("py.typed", "runtime.pyi", "_page.pyi", "pyppeteer.pyi", "playwright/sync_api.pyi", "playwright/async_api.pyi"):
            if not (package / expected).is_file():
                raise SystemExit(f"Installed wheel omitted {expected}")
        for filename in ("positive.py", "negative.py"):
            shutil.copyfile(root / "tests/typing" / filename, directory / filename)
        command = [args.python, "-m", "mypy", "--strict", "--ignore-missing-imports"]
        subprocess.run([*command, "positive.py"], cwd=directory, check=True)
        negative = subprocess.run([*command, "negative.py"], cwd=directory, capture_output=True, text=True)
        expected_lines = (6, 7, 8, 9, 10, 11, 14, 15, 16, 17)
        if negative.returncode != 1 or any(f"negative.py:{line}: error:" not in negative.stdout for line in expected_lines):
            raise SystemExit("Negative typing cases were not all rejected:\n" + negative.stdout + negative.stderr)
        origin = "Source" if args.allow_source else "Installed wheel"
        print(f"{origin} typing: valid consumers pass; all 10 invalid cases rejected")


if __name__ == "__main__":
    main()
