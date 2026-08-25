from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run(cwd: Path, *argv: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "maintenance.auto",
            "GIT_CONFIG_VALUE_0": "0",
            "GIT_CONFIG_KEY_1": "gc.auto",
            "GIT_CONFIG_VALUE_1": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        list(argv), cwd=cwd, env=environment, text=True, capture_output=True, check=True
    ).stdout.strip()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(path, "git", "init", "--template=", "-b", "main")
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
