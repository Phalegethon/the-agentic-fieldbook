#!/usr/bin/env python3
"""Stable plugin entrypoint for the TAF repository-context controller."""

from __future__ import annotations

from pathlib import Path
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = PLUGIN_ROOT / "tools" / "taf-context"
if not PACKAGE_ROOT.is_dir():
    sys.stderr.write("error: install the complete TAF plugin to use repository context\n")
    raise SystemExit(2)
sys.path.insert(0, str(PACKAGE_ROOT))

from taf_context.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["prepare", *sys.argv[1:]]))
