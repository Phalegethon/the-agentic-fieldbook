"""Executable packaging contract for the work-recovery skill."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.taf_context.repo_factory import init_committed_repo, write


ROOT = Path(__file__).parents[2]
SKILL = ROOT / "skills" / "work-recovery"
VENDOR = ROOT / "scripts" / "vendor-work-recovery-runtime"
RUNTIME_SOURCES = {
    "taf_context/__init__.py": "tools/taf-context/taf_context/__init__.py",
    "taf_context/git_snapshot.py": "tools/taf-context/taf_context/git_snapshot.py",
    "taf_context/models.py": "tools/taf-context/taf_context/models.py",
    "taf_context/recovery.py": "tools/taf-context/taf_context/recovery.py",
    "taf_context/recovery_models.py": "tools/taf-context/taf_context/recovery_models.py",
}


class SkillShapeTests(unittest.TestCase):
    def test_skill_has_discoverable_frontmatter_and_required_resources(self) -> None:
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_text.startswith("---\nname: work-recovery\n"))
        frontmatter = skill_text.split("---\n", 2)[1]
        description = next(
            line.removeprefix("description: ")
            for line in frontmatter.splitlines()
            if line.startswith("description: ")
        )
        self.assertTrue(description.startswith("Use when "))
        self.assertLessEqual(len(frontmatter), 1024)
        for relative in (
            "agents/openai.yaml",
            "references/recovery-contract.md",
            "references/continuation-contract.md",
            "references/context-actions.md",
            "references/tool-setup.md",
            "scripts/collect_recovery.py",
            "scripts/runtime-manifest.json",
        ):
            self.assertTrue((SKILL / relative).is_file(), relative)

    def test_agent_metadata_is_skill_specific_without_a_child_plugin(self) -> None:
        self.assertFalse((SKILL / ".claude-plugin" / "plugin.json").exists())
        agent = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('$work-recovery', agent)


class VendoredRuntimeTests(unittest.TestCase):
    def test_manifest_and_vendored_bytes_match_the_allowlisted_engine_source(self) -> None:
        manifest = json.loads((SKILL / "scripts" / "runtime-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "1")
        records = manifest["files"]
        self.assertEqual([record["destination"] for record in records], sorted(RUNTIME_SOURCES))
        self.assertEqual({record["destination"]: record["source"] for record in records}, RUNTIME_SOURCES)
        for record in records:
            source = ROOT / record["source"]
            destination = SKILL / "scripts" / record["destination"]
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(record["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_vendor_check_is_read_only_and_reports_no_drift(self) -> None:
        before = {
            path.relative_to(SKILL): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in SKILL.rglob("*")
            if path.is_file()
        }
        completed = subprocess.run(
            [str(VENDOR), "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        after = {
            path.relative_to(SKILL): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in SKILL.rglob("*")
            if path.is_file()
        }
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "work-recovery runtime is current\n")
        self.assertEqual(before, after)

    def test_copied_skill_collects_once_without_repository_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copied_skill = root / "work-recovery"
            shutil.copytree(SKILL, copied_skill)
            skill_files_before = {
                path.relative_to(copied_skill): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in copied_skill.rglob("*")
                if path.is_file()
            }
            repo = init_committed_repo(root / "fixture")
            write(repo / "tracked.txt", "interrupted\n")

            completed = subprocess.run(
                [
                    "python3",
                    "-X",
                    "pycache_prefix=",
                    str(copied_skill / "scripts" / "collect_recovery.py"),
                    "--repo",
                    str(repo),
                    "--base",
                    "main",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            envelope = json.loads(completed.stdout)
            self.assertEqual(envelope["schema_version"], "1")
            self.assertEqual(envelope["collection_count"], 1)
            self.assertEqual(envelope["dossier"]["current"]["state"], "active-dirty")
            self.assertEqual(envelope["dossier"]["coverage"]["budget_characters"], 2000)
            self.assertLessEqual(envelope["characters_used"], 2000)
            self.assertFalse(any(path.name.startswith(".taf") for path in repo.iterdir()))
            skill_files_after = {
                path.relative_to(copied_skill): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in copied_skill.rglob("*")
                if path.is_file()
            }
            self.assertEqual(skill_files_after, skill_files_before)


if __name__ == "__main__":
    unittest.main()
