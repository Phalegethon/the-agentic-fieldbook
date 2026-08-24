from __future__ import annotations

import subprocess
from pathlib import Path


def run(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run(path, "git", "init", "-b", "main")
    run(path, "git", "config", "user.email", "fixture@example.invalid")
    run(path, "git", "config", "user.name", "Fixture")
    write(path / "README.md", "fixture\n")
    run(path, "git", "add", "README.md")
    run(path, "git", "commit", "-m", "init fixture")
    return path


def commit_all(repo: Path, message: str) -> str:
    run(repo, "git", "add", "-A")
    run(repo, "git", "commit", "-m", message)
    return run(repo, "git", "rev-parse", "HEAD")


def build_behavior_repo(
    root: Path, *, large: bool = False, backend_only: bool = False
) -> Path:
    repo = init_repo(root)
    run(repo, "git", "switch", "-c", "feature/handoff")
    write(repo / "src/api/orders.py", "def complete_order(order):\n    return order\n")
    write(repo / "migrations/001_add_order_state.sql", "ALTER TABLE orders ADD state TEXT;\n")
    if not backend_only:
        write(repo / "src/ui/checkout.tsx", "export const Checkout = () => 'done'\n")
    if large:
        for index in range(120):
            write(
                repo / "generated" / f"client_{index:03d}.ts",
                f"export const generated{index} = {index};\n" * 40,
            )
    commit_all(repo, "implement order completion")
    return repo
