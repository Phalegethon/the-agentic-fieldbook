#!/usr/bin/env python3
"""Stable entry point for the TAF repo-context MCP stdio server."""

from __future__ import annotations

from pathlib import Path
import sys


PACKAGE_ROOT = Path(__file__).resolve().parent
if not (PACKAGE_ROOT / "taf_context").is_dir():
    sys.stderr.write("error: install the complete TAF plugin to use the repo-context server\n")
    raise SystemExit(2)
sys.path.insert(0, str(PACKAGE_ROOT))

from taf_context.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
