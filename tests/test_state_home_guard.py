"""The test process must never touch the real user-local TAF state."""

from __future__ import annotations

import os
from pathlib import Path
import re
import unittest

import tests

ROOT = Path(__file__).parents[1]


class StateHomeGuardTests(unittest.TestCase):
    def test_importing_tests_pins_state_home_to_a_temporary_directory(self) -> None:
        value = os.environ.get("TAF_STATE_HOME")
        self.assertTrue(value, "TAF_STATE_HOME must be set by tests/__init__.py")
        guard = tests.install_state_home_guard()
        self.assertEqual(Path(value), guard)
        self.assertTrue(guard.is_dir())
        self.assertNotIn("Library/Application Support/TAF", value)
        self.assertNotIn(".local/state/taf", value)

    def test_no_test_calls_the_cli_with_the_process_environment(self) -> None:
        offenders = []
        for path in sorted((ROOT / "tests").rglob("*.py")):
            if path.name == "test_state_home_guard.py":
                continue
            text = path.read_text(encoding="utf-8")
            if CLI_IMPORT not in text and "run_prepare_command" not in text:
                continue
            for match in CALL.finditer(text):
                call = _call_text(text, match.end() - 1)
                if "environment=" not in call:
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(offenders, [])


CLI_IMPORT = "from taf_context.cli import main"
# A call to the broker entry point or the prepare runner. Excludes ``def main(``
# and attribute calls such as ``unittest.main(``.
CALL = re.compile(r"(?<!def )(?<![\w.])(main|run_prepare_command)\(")


def _call_text(text: str, open_paren: int) -> str:
    """Return the source text of one call from its opening parenthesis."""
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren : index + 1]
    return text[open_paren:]


if __name__ == "__main__":
    unittest.main()
