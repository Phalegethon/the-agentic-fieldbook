"""Index-free bounded fallback tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taf_context.bounded_fallback import FallbackPolicy, run_bounded_fallback
from taf_context.level1_models import Level1Request

from .test_level1_models import request_wire


class BoundedFallbackTests(unittest.TestCase):
    def test_symbol_search_reads_bounded_files_and_returns_current_citation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src/app.py").write_text(
                "class RecoveryDossier:\n    pass\n", encoding="utf-8"
            )
            (root / "src/noise.py").write_text("class Noise:\n    pass\n", encoding="utf-8")
            wire = request_wire()
            wire["provider_identity"] = "taf.bounded-fallback"
            wire["index_identity"] = "sha256:" + "9" * 64
            wire["filters"]["path_prefixes"] = ["src"]
            request = Level1Request.from_dict(wire)
            result, evidence = run_bounded_fallback(
                request, root, ("src/app.py", "src/noise.py"),
                FallbackPolicy(4, 4096, 256, 16),
            )
            self.assertEqual(result.returned_count, 1)
            self.assertEqual(result.findings[0].path, "src/app.py")
            self.assertEqual(result.findings[0].start_line, 1)
            self.assertLessEqual(evidence.files_read, 4)
            self.assertLessEqual(evidence.bytes_read, 4096)

    def test_secret_generated_binary_and_symlink_content_are_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "vendor").mkdir()
            (root / "src/app.py").write_text(
                "api_key=supersecret\nclass RecoveryDossier:\n    pass\n",
                encoding="utf-8",
            )
            (root / "vendor/app.py").write_text("class RecoveryDossier: pass\n")
            (root / "src/binary.py").write_bytes(b"\x00RecoveryDossier")
            (root / "src/escape.py").symlink_to("/etc/passwd")
            wire = request_wire()
            wire["provider_identity"] = "taf.bounded-fallback"
            wire["index_identity"] = "sha256:" + "9" * 64
            wire["filters"]["path_prefixes"] = []
            request = Level1Request.from_dict(wire)
            result, evidence = run_bounded_fallback(
                request, root,
                ("src/app.py", "src/binary.py", "src/escape.py", "vendor/app.py"),
                FallbackPolicy(8, 8192, 256, 16),
            )
            self.assertEqual(tuple(item.path for item in result.findings), ("src/app.py",))
            self.assertNotIn("supersecret", result.findings[0].preview)
            self.assertGreaterEqual(evidence.excluded_paths, 3)

    def test_byte_ceiling_stops_with_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            for index in range(10):
                path = f"src/file{index}.py"
                (root / "src").mkdir(exist_ok=True)
                (root / path).write_text("x" * 200, encoding="utf-8")
                paths.append(path)
            wire = request_wire()
            wire["provider_identity"] = "taf.bounded-fallback"
            wire["index_identity"] = "sha256:" + "9" * 64
            wire["filters"]["path_prefixes"] = []
            request = Level1Request.from_dict(wire)
            result, evidence = run_bounded_fallback(
                request, root, tuple(paths), FallbackPolicy(10, 300, 128, 16)
            )
            self.assertLessEqual(evidence.bytes_read, 300)
            self.assertLess(result.coverage.path_coverage, 1.0)
            self.assertIn("fallback-budget-exhausted", result.warnings)


if __name__ == "__main__":
    unittest.main()
