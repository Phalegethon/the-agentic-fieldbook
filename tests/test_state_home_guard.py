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
            pattern = _broker_call_pattern(text)
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                call = _call_text(text, match.end() - 1)
                if "environment=" not in call:
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(offenders, [])

    def test_call_pattern_follows_import_aliases(self) -> None:
        aliased = "from taf_context.cli import main as context_main\ncontext_main([])\n"
        plain = "from taf_context.cli import main\nmain([])\nunittest.main()\ndef main(): pass\n"
        self.assertEqual(
            [m.group(0) for m in _broker_call_pattern(aliased).finditer(aliased)], ["context_main("]
        )
        self.assertEqual(
            [m.group(0) for m in _broker_call_pattern(plain).finditer(plain)], ["main("]
        )
        self.assertIsNone(_broker_call_pattern("import os\n"))


CLI_IMPORT = re.compile(
    r"^\s*from taf_context\.cli import main(?: as (?P<alias>\w+))?\s*$", re.MULTILINE
)


def _broker_call_pattern(text: str) -> "re.Pattern[str] | None":
    """Build the call regex for one file from its broker imports, or None."""
    names = {"run_prepare_command"} if "run_prepare_command" in text else set()
    for match in CLI_IMPORT.finditer(text):
        names.add(match.group("alias") or "main")
    if not names:
        return None
    alternatives = "|".join(sorted(re.escape(name) for name in names))
    # Excludes ``def <name>(`` and attribute calls such as ``unittest.main(``.
    return re.compile(rf"(?<!def )(?<![\w.])(?:{alternatives})\(")


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
