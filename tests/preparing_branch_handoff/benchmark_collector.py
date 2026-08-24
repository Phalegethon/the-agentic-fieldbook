from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from tests.preparing_branch_handoff.repo_factory import commit_all, init_repo, run, write


SCRIPT = ROOT / "skills" / "branch-handoff" / "scripts" / "collect_diff.py"


def build_repo(root: Path, files: int, lines: int, generated_ratio: float) -> Path:
    repo = init_repo(root)
    base = run(repo, "git", "rev-parse", "main")
    run(repo, "git", "switch", "-c", "feature")
    generated_count = int(files * generated_ratio)
    for index in range(files):
        directory = "generated" if index < generated_count else "src/area"
        write(
            repo / directory / f"file_{index:04d}.py",
            f"value_{index} = {index}\n" * lines,
        )
    commit_all(repo, "benchmark diff")
    run(repo, "git", "tag", "benchmark-base", base)
    return repo


def measure(name: str, files: int, lines: int, generated_ratio: float) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repo(Path(tmp) / "repo", files, lines, generated_ratio)
        started = time.perf_counter()
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repo",
                str(repo),
                "--base",
                "benchmark-base",
                "--head",
                "HEAD",
                "--offline",
                "--max-patch-chars",
                "120000",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        elapsed = time.perf_counter() - started
        manifest = json.loads(result.stdout)
        return {
            "name": name,
            "files": files,
            "lines_per_file": lines,
            "elapsed_seconds": round(elapsed, 3),
            "dossier_chars": manifest["dossier_chars"],
            "ledger_files": manifest["file_count"],
        }


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "results": [
                    measure("small", 10, 20, 0.0),
                    measure("medium", 60, 40, 0.15),
                    measure("large", 500, 80, 0.65),
                ],
            },
            indent=2,
        )
    )
