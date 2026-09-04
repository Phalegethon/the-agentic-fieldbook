#!/usr/bin/env python3
"""Package one native TAF runtime with a deterministic checksum sidecar."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import sys


ENGINE_VERSION = "0.6.0"
SUPPORTED_TARGETS = {
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("windows", "amd64"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="package-native-release")
    parser.add_argument("--binary", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    target = (args.system, args.arch)
    if target not in SUPPORTED_TARGETS:
        parser.error("unsupported release target")
    source = Path(args.binary)
    if not source.is_file():
        parser.error("native binary is unavailable")

    suffix = ".exe" if args.system == "windows" else ""
    name = f"taf-level1_{ENGINE_VERSION}_{args.system}_{args.arch}{suffix}"
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    asset = output / name
    shutil.copyfile(source, asset)
    if args.system != "windows":
        asset.chmod(0o755)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    checksum = output / f"{name}.sha256"
    checksum.write_bytes(f"{digest}  {name}\n".encode("ascii"))
    sys.stdout.write(f"{asset}\n{checksum}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
