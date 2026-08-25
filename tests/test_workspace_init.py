#!/usr/bin/env python3
"""Initialization: what the plan sees, what --apply writes, and what it refuses.

Every case builds a disposable father directory with its own toolkit copy and its
own child repositories. The developer's real siblings are never touched — the
whole point of the planner is that it is safe against a real tree, and a test
that proved it by using one would be proving it the wrong way round.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_config  # noqa: E402
import workspace_init  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example"},
    )


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    """Every file under a tree, by content digest. The evidence for 'wrote nothing'.

    `__pycache__` is excluded: CPython writes it whenever a module is imported,
    which happens no matter what the planner then decides to do. It is the
    interpreter's artifact rather than the planner's, and counting it would make
    this assertion fail for a reason that has nothing to do with the claim.
    `.git` is excluded because Git rewrites index metadata on read commands.
    """
    skip = {".git", "__pycache__"}
    found: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        if skip & set(item.parts):
            continue
        if item.is_file():
            found[str(item.relative_to(root))] = digest(item)
        elif item.is_dir():
            found.setdefault(str(item.relative_to(root)) + "/", "dir")
    return found


class InitCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.father = Path(self._tmp.name) / "father"
        self.toolkit = self.father / "toolkit"
        (self.toolkit / "scripts").mkdir(parents=True)
        # A toolkit copy that is real enough to plan with, and nothing more.
        for name in ("workspace_config.py", "workspace_init.py"):
            (self.toolkit / "scripts" / name).write_bytes(
                (ROOT / "scripts" / name).read_bytes()
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def plan(self, **kwargs):
        kwargs.setdefault("product_id", "example")
        kwargs.setdefault("product_name", "Example Product")
        kwargs.setdefault("data_dir", None)
        return workspace_init.build_plan(
            self.toolkit, kwargs["product_id"], kwargs["product_name"], kwargs["data_dir"]
        )


class TestPlanningWritesNothing(InitCase):
    def test_planning_creates_no_file_and_no_directory(self) -> None:
        make_repo(self.father / "example-api")
        (self.father / "plain-directory").mkdir()
        before = snapshot(self.father)
        plan = self.plan()
        workspace_init.render(plan)
        self.assertEqual(snapshot(self.father), before)
        self.assertFalse((self.toolkit / "workspace.json").exists())
        self.assertFalse((self.father / "workspace-data").exists())

    def test_planning_through_init_sh_creates_nothing(self) -> None:
        """The operator-facing path, not just the module."""
        make_repo(self.father / "example-api")
        before = snapshot(self.father)
        result = subprocess.run(
            [sys.executable, str(self.toolkit / "scripts" / "workspace_init.py"),
             "--toolkit-root", str(self.toolkit), "--product", "example"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLAN ONLY", result.stdout)
        self.assertEqual(snapshot(self.father), before)


class TestDiscovery(InitCase):
    def test_immediate_sibling_git_repositories_are_found(self) -> None:
        make_repo(self.father / "example-api")
        make_repo(self.father / "example-web")
        aliases = [alias for alias, _ in self.plan().repositories]
        self.assertEqual(aliases, ["example-api", "example-web"])

    def test_a_repository_nested_below_a_sibling_is_ignored(self) -> None:
        outer = make_repo(self.father / "example-api")
        make_repo(outer / "vendor" / "inner")
        aliases = [alias for alias, _ in self.plan().repositories]
        self.assertEqual(aliases, ["example-api"])

    def test_a_non_git_sibling_is_ignored(self) -> None:
        make_repo(self.father / "example-api")
        (self.father / "notes").mkdir()
        plan = self.plan()
        self.assertEqual([alias for alias, _ in plan.repositories], ["example-api"])
        self.assertIn(
            "notes", [path.name for path, _ in plan.ignored]
        )

    def test_the_toolkit_excludes_itself(self) -> None:
        make_repo(self.toolkit)
        make_repo(self.father / "example-api")
        plan = self.plan()
        self.assertEqual([alias for alias, _ in plan.repositories], ["example-api"])
        self.assertIn(
            ("toolkit", "this toolkit repository"),
            [(path.name, why) for path, why in plan.ignored],
        )

    def test_the_data_directory_is_excluded_even_when_it_is_a_repository(self) -> None:
        make_repo(self.father / "example-api")
        make_repo(self.father / "workspace-data")
        plan = self.plan()
        self.assertEqual([alias for alias, _ in plan.repositories], ["example-api"])
        self.assertIn(
            ("workspace-data", "the configured data directory"),
            [(path.name, why) for path, why in plan.ignored],
        )

    def test_a_custom_data_directory_is_excluded_too(self) -> None:
        make_repo(self.father / "example-api")
        make_repo(self.father / "operational")
        plan = self.plan(data_dir="../operational")
        self.assertEqual([alias for alias, _ in plan.repositories], ["example-api"])

    def test_a_git_file_counts_as_a_repository_root(self) -> None:
        """A linked worktree and a submodule both have `.git` as a FILE."""
        worktree = self.father / "linked-worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
        self.assertEqual(
            [alias for alias, _ in self.plan().repositories], ["linked-worktree"]
        )


class TestAliasProposal(InitCase):
    def test_aliases_are_deterministic_from_the_basename(self) -> None:
        self.assertEqual(workspace_init.propose_alias("example-api"), "example-api")
        self.assertEqual(workspace_init.propose_alias("Example API"), "example-api")
        self.assertEqual(workspace_init.propose_alias("my_worker"), "my-worker")
        self.assertEqual(workspace_init.propose_alias("Web.Client"), "web-client")

    def test_the_same_tree_proposes_the_same_aliases_twice(self) -> None:
        for name in ("example-api", "example-web", "my worker"):
            make_repo(self.father / name)
        first = [alias for alias, _ in self.plan().repositories]
        second = [alias for alias, _ in self.plan().repositories]
        self.assertEqual(first, second)
        self.assertEqual(first, ["example-api", "example-web", "my-worker"])

    def test_colliding_proposals_are_made_unique_deterministically(self) -> None:
        make_repo(self.father / "web client")
        make_repo(self.father / "web-client")
        aliases = [alias for alias, _ in self.plan().repositories]
        self.assertEqual(len(set(aliases)), 2)
        self.assertIn("web-client", aliases)
        self.assertIn("web-client-2", aliases)


class TestApply(InitCase):
    def apply(self, **kwargs):
        plan = self.plan(**kwargs)
        return plan, workspace_init.apply_plan(plan)

    def test_apply_creates_the_configuration_and_the_data_skeleton(self) -> None:
        make_repo(self.father / "example-api")
        plan, _ = self.apply()
        config = self.toolkit / "workspace.json"
        self.assertTrue(config.is_file())
        payload = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], workspace_config.SCHEMA)
        self.assertEqual(payload["product"]["id"], "example")
        self.assertEqual(payload["data_root"], "../workspace-data")
        self.assertEqual(payload["repositories"],
                         [{"alias": "example-api", "path": "../example-api"}])
        data = self.father / "workspace-data"
        for relative in workspace_config.DATA_SUBDIRS:
            self.assertTrue((data / relative).is_dir(), relative)
        self.assertTrue((data / "LLM" / "prompts" / "example-api" / "done").is_dir())
        self.assertTrue((data / "LLM" / "handoffs" / "example-api").is_dir())

    def test_the_written_configuration_loads(self) -> None:
        make_repo(self.father / "example-api")
        self.apply()
        workspace = workspace_config.load(self.toolkit / "workspace.json")
        self.assertEqual(workspace.require("example-api").path.resolve(),
                         (self.father / "example-api").resolve())

    def test_apply_creates_only_missing_seed_documents(self) -> None:
        make_repo(self.father / "example-api")
        existing = self.toolkit / "README.md"
        existing.write_text("MY OWN README\n", encoding="utf-8")
        before = digest(existing)
        self.apply()
        self.assertEqual(digest(existing), before)
        for name in workspace_init.TOOLKIT_SEEDS:
            self.assertTrue((self.toolkit / name).is_file(), name)

    def test_existing_design_documents_stay_byte_identical(self) -> None:
        repo = make_repo(self.father / "example-api")
        originals = {}
        for name in ("AGENTS.md", "DESIGN.md", "UI-DESIGN.md"):
            for target in (self.toolkit / name, repo / name):
                target.write_text(f"hand written {name}\n", encoding="utf-8")
                originals[str(target)] = digest(target)
        self.apply()
        for path, before in originals.items():
            self.assertEqual(digest(Path(path)), before, path)

    def test_ui_design_is_not_seeded_into_child_repositories(self) -> None:
        """A workspace having a UI does not mean every repository in it does."""
        repo = make_repo(self.father / "example-api")
        self.apply()
        self.assertTrue((self.toolkit / "UI-DESIGN.md").is_file())
        self.assertFalse((repo / "UI-DESIGN.md").exists())

    def test_child_repositories_get_a_pointer_and_a_design_seed(self) -> None:
        repo = make_repo(self.father / "example-api")
        self.apply()
        agents = (repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("WORKFLOW.md", agents)
        self.assertIn("example-api", agents)
        self.assertTrue((repo / "DESIGN.md").is_file())

    def test_child_repositories_are_never_committed(self) -> None:
        repo = make_repo(self.father / "example-api")
        head_before = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        self.apply()
        head_after = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True).stdout
        # The seeds are there, and they are UNCOMMITTED — which is exactly what
        # the report warns about.
        self.assertIn("AGENTS.md", status)
        self.assertIn("DESIGN.md", status)

    def test_no_remote_is_added_or_changed(self) -> None:
        repo = make_repo(self.father / "example-api")
        before = subprocess.run(["git", "-C", str(repo), "remote", "-v"],
                                capture_output=True, text=True).stdout
        self.apply()
        after = subprocess.run(["git", "-C", str(repo), "remote", "-v"],
                               capture_output=True, text=True).stdout
        self.assertEqual(before, after)

    def test_a_second_identical_apply_is_a_no_op(self) -> None:
        make_repo(self.father / "example-api")
        self.apply()
        after_first = snapshot(self.father)
        plan, created = self.apply()
        self.assertEqual(created, [])
        self.assertEqual(snapshot(self.father), after_first)

    def test_absolute_data_directory_is_stored_absolute(self) -> None:
        make_repo(self.father / "example-api")
        elsewhere = Path(self._tmp.name) / "elsewhere-data"
        self.apply(data_dir=str(elsewhere))
        payload = json.loads((self.toolkit / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["data_root"], str(elsewhere))
        self.assertTrue((elsewhere / "LLM" / "prompts").is_dir())

    def test_a_path_with_spaces_survives_apply(self) -> None:
        make_repo(self.father / "my worker")
        self.apply()
        workspace = workspace_config.load(self.toolkit / "workspace.json")
        self.assertTrue(workspace.require("my-worker").path.is_dir())
        self.assertTrue(
            (self.father / "workspace-data" / "LLM" / "prompts" / "my-worker").is_dir()
        )


class TestExistingConfigurationIsAuthoritative(InitCase):
    def test_an_existing_configuration_is_never_regenerated(self) -> None:
        make_repo(self.father / "example-api")
        config = self.toolkit / "workspace.json"
        config.write_text(json.dumps({
            "schema": workspace_config.SCHEMA,
            "product": {"id": "chosen", "name": "Chosen By Operator"},
            "data_root": "../my-data",
            "repositories": [{"alias": "MYNAME", "path": "../example-api"}],
        }, indent=2), encoding="utf-8")
        before = digest(config)
        plan = self.plan(product_id="different", product_name="Different")
        workspace_init.apply_plan(plan)
        self.assertEqual(digest(config), before)
        self.assertEqual(plan.product_id, "chosen")
        self.assertEqual([alias for alias, _ in plan.repositories], ["MYNAME"])

    def test_operator_aliases_survive_and_drive_the_data_skeleton(self) -> None:
        make_repo(self.father / "example-api")
        (self.toolkit / "workspace.json").write_text(json.dumps({
            "schema": workspace_config.SCHEMA,
            "product": {"id": "chosen", "name": "Chosen"},
            "data_root": "../my-data",
            "repositories": [{"alias": "MYNAME", "path": "../example-api"}],
        }, indent=2), encoding="utf-8")
        workspace_init.apply_plan(self.plan())
        self.assertTrue((self.father / "my-data" / "LLM" / "prompts" / "MYNAME").is_dir())

    def test_an_unconfigured_sibling_is_reported_not_added(self) -> None:
        make_repo(self.father / "example-api")
        make_repo(self.father / "example-web")
        (self.toolkit / "workspace.json").write_text(json.dumps({
            "schema": workspace_config.SCHEMA,
            "product": {"id": "chosen", "name": "Chosen"},
            "data_root": "../workspace-data",
            "repositories": [{"alias": "api", "path": "../example-api"}],
        }, indent=2), encoding="utf-8")
        plan = self.plan()
        self.assertEqual([alias for alias, _ in plan.repositories], ["api"])
        self.assertTrue(any("example-web" in line for line in plan.discrepancies))

    def test_a_configured_repository_that_is_missing_is_reported(self) -> None:
        (self.toolkit / "workspace.json").write_text(json.dumps({
            "schema": workspace_config.SCHEMA,
            "product": {"id": "chosen", "name": "Chosen"},
            "data_root": "../workspace-data",
            "repositories": [{"alias": "gone", "path": "../never-cloned"}],
        }, indent=2), encoding="utf-8")
        plan = self.plan()
        self.assertTrue(any("never-cloned" in line for line in plan.discrepancies))


class TestSeedHonesty(InitCase):
    def test_seeds_state_facts_and_mark_the_rest_TODO(self) -> None:
        make_repo(self.father / "example-api")
        self.apply = None  # not used here
        plan = self.plan()
        workspace_init.apply_plan(plan)
        design = (self.toolkit / "DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("TODO", design)
        self.assertIn("example-api", design)
        # It must not have invented a role for a repository it only saw the name of.
        for invented in ("REST", "frontend", "database", "microservice", "deploys"):
            self.assertNotIn(invented, design)

    def test_seeds_name_the_product_and_the_data_root(self) -> None:
        make_repo(self.father / "example-api")
        workspace_init.apply_plan(self.plan())
        readme = (self.toolkit / "README.md").read_text(encoding="utf-8")
        self.assertIn("Example Product", readme)
        self.assertIn("workspace-data", readme)


if __name__ == "__main__":
    unittest.main()
