#!/usr/bin/env python3
"""Standalone one-shot wrapper for the vendored recovery runtime."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))

from taf_context.models import canonical_json  # noqa: E402
from taf_context.recovery import RecoveryRequest, collect_recovery  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base")
    parser.add_argument("--max-output-chars", type=int, choices=(2000, 4000, 8000, 12000), default=2000)
    parser.add_argument("--include-untracked", action="append", default=[])
    parser.add_argument("--note-file", action="append", default=[])
    parser.add_argument("--test-results-file", action="append", default=[])
    args = parser.parse_args()
    try:
        result = collect_recovery(
            RecoveryRequest(
                repo=Path(args.repo),
                base=args.base,
                max_chars=args.max_output_chars,
                untracked_content_paths=tuple(args.include_untracked),
                note_files=tuple(Path(value) for value in args.note_file),
                test_result_files=tuple(Path(value) for value in args.test_results_file),
            )
        )
        sys.stdout.write(
            canonical_json(
                {
                    "schema_version": "1",
                    "collection_count": 1,
                    "characters_used": result.characters_used,
                    "dossier": result.dossier.to_dict(),
                    "model_text": result.model_text,
                }
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        message = " ".join(str(error).splitlines()).strip() or "recovery failed"
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
