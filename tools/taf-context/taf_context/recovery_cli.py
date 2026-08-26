"""Command-line adapter for zero-artifact work recovery."""

from __future__ import annotations

import argparse
from pathlib import Path

from .models import canonical_json
from .recovery import RecoveryRequest, collect_recovery


def register_recovery_command(subparsers: argparse._SubParsersAction) -> None:
    """Register the stdout-only ``recover`` command."""
    recover = subparsers.add_parser("recover")
    recover.add_argument("--repo", required=True)
    recover.add_argument("--base")
    recover.add_argument(
        "--max-output-chars",
        type=int,
        choices=(2000, 4000, 8000, 12000),
        default=4000,
    )
    recover.add_argument("--include-untracked", action="append", default=[])
    recover.add_argument("--note-file", action="append", default=[])
    recover.add_argument("--test-results-file", action="append", default=[])
    recover.add_argument("--format", choices=("text", "json"), default="text")


def run_recovery_command(args: argparse.Namespace) -> str:
    """Collect once and return plain evidence text or canonical dossier JSON."""
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
    if args.format == "json":
        return canonical_json(result.dossier.to_dict())
    return result.model_text
