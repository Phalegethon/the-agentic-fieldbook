from __future__ import annotations

import subprocess
from pathlib import Path


def run(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        list(argv), cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(path, "git", "init", "-b", "main")
    run(path, "git", "config", "user.email", "fixture@example.invalid")
    run(path, "git", "config", "user.name", "Fixture")
    return path


def commit_all(repo: Path, message: str = "fixture") -> str:
    run(repo, "git", "add", "-A")
    run(repo, "git", "commit", "-m", message)
    return run(repo, "git", "rev-parse", "HEAD")


def init_committed_repo(path: Path) -> Path:
    repo = init_repo(path)
    write(repo / "tracked.txt", "tracked\n")
    commit_all(repo)
    return repo
