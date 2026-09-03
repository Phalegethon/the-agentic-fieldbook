"""Behavioral tests for native runtime release packaging."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[2]
PACKAGER = ROOT / "scripts" / "package-native-release.py"


class NativeReleasePackageTests(unittest.TestCase):
    def test_packager_emits_versioned_binary_and_matching_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "taf-level1"
            payload = b"native-runtime\x00payload"
            binary.write_bytes(payload)
            output = root / "release"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "--binary",
                    str(binary),
                    "--system",
                    "darwin",
                    "--arch",
                    "arm64",
                    "--output-dir",
                    str(output),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual((completed.returncode, completed.stderr), (0, ""))
            asset = output / "taf-level1_0.3.0_darwin_arm64"
            checksum = output / f"{asset.name}.sha256"
            self.assertEqual(asset.read_bytes(), payload)
            self.assertEqual(
                checksum.read_bytes(),
                f"{hashlib.sha256(payload).hexdigest()}  {asset.name}\n".encode(
                    "ascii"
                ),
            )
            self.assertEqual(completed.stdout, f"{asset}\n{checksum}\n")

    def test_windows_checksum_uses_portable_lf_line_ending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "taf-level1.exe"
            payload = b"windows-native-runtime"
            binary.write_bytes(payload)
            output = root / "release"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "--binary",
                    str(binary),
                    "--system",
                    "windows",
                    "--arch",
                    "amd64",
                    "--output-dir",
                    str(output),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual((completed.returncode, completed.stderr), (0, ""))
            asset = output / "taf-level1_0.3.0_windows_amd64.exe"
            checksum = output / f"{asset.name}.sha256"
            self.assertEqual(
                checksum.read_bytes(),
                f"{hashlib.sha256(payload).hexdigest()}  {asset.name}\n".encode(
                    "ascii"
                ),
            )

    def test_packager_rejects_an_unpublished_platform_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "taf-level1"
            binary.write_bytes(b"runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGER),
                    "--binary",
                    str(binary),
                    "--system",
                    "windows",
                    "--arch",
                    "arm64",
                    "--output-dir",
                    str(root / "release"),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("unsupported release target", completed.stderr)
            self.assertFalse((root / "release").exists())


if __name__ == "__main__":
    unittest.main()
