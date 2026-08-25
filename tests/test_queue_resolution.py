#!/usr/bin/env python3
"""Prompt selection, dependencies, handoffs and timing — over a configured workspace.

The behaviour under test is the source toolkit's, unchanged. What changed is
where the queue's identity comes from: the configured alias, rather than a file
committed inside each repository. These cases exercise that seam and the rules
that hang off it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agent_task  # noqa: E402
import execution_time  # noqa: E402
import resolve_next_prompt as resolver  # noqa: E402
import sequence_plan  # noqa: E402
import workspace_config  # noqa: E402


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example",
}


class WorkspaceCase(unittest.TestCase):
    """A disposable father directory with a toolkit, repositories and a data root."""

    aliases = ("api", "web")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.father = Path(self._tmp.name) / "father"
        self.toolkit = self.father / "toolkit"
        self.toolkit.mkdir(parents=True)
        self.data = self.father / "workspace-data"
        self.repos: dict[str, Path] = {}
        for alias in self.aliases:
            repo = self.father / f"example-{alias}"
            repo.mkdir(parents=True)
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                           check=True, capture_output=True, env=GIT_ENV)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
                            "-m", "init"], check=True, capture_output=True, env=GIT_ENV)
            self.repos[alias] = repo
            for kind in ("prompts", "handoffs"):
                (self.data / "LLM" / kind / alias).mkdir(parents=True)
            (self.data / "LLM" / "prompts" / alias / "done").mkdir()
            (self.data / "LLM" / "prompts" / alias / "blocked").mkdir()
        self.config_path = self.toolkit / "workspace.json"
        self.config_path.write_text(json.dumps({
            "schema": workspace_config.SCHEMA,
            "product": {"id": "example", "name": "Example Product"},
            "data_root": "../workspace-data",
            "repositories": [
                {"alias": alias, "path": f"../example-{alias}"} for alias in self.aliases
            ],
        }, indent=2), encoding="utf-8")
        self.workspace = workspace_config.load(self.config_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def queue(self, alias: str) -> Path:
        return self.data / "LLM" / "prompts" / alias

    def add_prompt(self, alias: str, name: str, frontmatter: str = "", body: str = "work") -> Path:
        path = self.queue(alias) / name
        block = f"---\n{frontmatter}\n---\n" if frontmatter else ""
        path.write_text(f"{block}# {name}\n\n{body}\n", encoding="utf-8")
        return path

    def complete(self, alias: str, name: str) -> None:
        (self.queue(alias) / name).rename(self.queue(alias) / "done" / name)


class TestSelection(WorkspaceCase):
    def test_the_oldest_queued_prompt_is_next(self) -> None:
        self.add_prompt("api", "20260801_02_Second.md")
        self.add_prompt("api", "20260801_01_First.md")
        state = resolver.resolve("api", self.data)
        self.assertEqual(state["action"], "run")
        self.assertTrue(state["prompt_path"].endswith("20260801_01_First.md"))

    def test_an_empty_queue_is_idle_not_an_error(self) -> None:
        state = resolver.resolve("api", self.data)
        self.assertEqual(state["action"], "idle")

    def test_a_prompt_in_done_is_never_selected_again(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        self.complete("api", "20260801_01_First.md")
        self.assertEqual(resolver.resolve("api", self.data)["action"], "idle")

    def test_queue_aliases_come_only_from_the_configuration(self) -> None:
        """A queue folder with no configured alias is not a queue. Discovery is
        the configuration's list intersected with what exists on disk."""
        (self.data / "LLM" / "prompts" / "stray").mkdir()
        (self.data / "LLM" / "prompts" / "stray" / "20260801_01_Ghost.md").write_text(
            "# ghost\n", encoding="utf-8")
        discovered = [
            repo["alias"] for repo in sequence_plan.discover_repositories(self.workspace)
        ]
        self.assertEqual(sorted(discovered), ["api", "web"])
        self.assertNotIn("stray", discovered)

    def test_skip_defers_without_touching_the_file(self) -> None:
        path = self.add_prompt("api", "20260801_01_First.md")
        self.add_prompt("api", "20260801_02_Second.md")
        state = resolver.resolve("api", self.data, skip={"20260801_01_First"})
        self.assertTrue(state["prompt_path"].endswith("20260801_02_Second.md"))
        self.assertTrue(path.is_file(), "a skipped prompt must not be moved")


class TestDependencies(WorkspaceCase):
    def test_a_same_repository_prerequisite_gates_selection(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        self.add_prompt("api", "20260801_02_Second.md",
                        frontmatter="requires:\n  - 20260801_01_First")
        # 01 is selected first; 02 alone would be blocked.
        self.assertTrue(
            resolver.resolve("api", self.data)["prompt_path"].endswith("01_First.md")
        )
        self.complete("api", "20260801_01_First.md")
        self.assertTrue(
            resolver.resolve("api", self.data)["prompt_path"].endswith("02_Second.md")
        )

    def test_a_cross_repository_prerequisite_uses_the_other_alias(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        self.add_prompt("web", "20260801_02_Second.md",
                        frontmatter="requires:\n  - repo: api\n    prompt: 20260801_01_First.md")
        blocked = resolver.resolve("web", self.data)
        self.assertEqual(blocked["action"], "blocked")
        self.assertIn("api", blocked["reason"])
        self.complete("api", "20260801_01_First.md")
        self.assertEqual(resolver.resolve("web", self.data)["action"], "run")

    def test_dependents_are_computed_across_every_configured_queue(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        self.add_prompt("web", "20260801_02_Second.md",
                        frontmatter="requires:\n  - repo: api\n    prompt: 20260801_01_First.md")
        self.add_prompt("web", "20260801_03_Third.md",
                        frontmatter="requires:\n  - 20260801_02_Second")
        result = resolver.dependents_of(self.data, ["api/20260801_01_First"])
        stems = sorted(entry["stem"] for entry in result["dependents"])
        self.assertEqual(stems, ["20260801_02_Second", "20260801_03_Third"])

    def test_unreadable_frontmatter_blocks_rather_than_reads_as_no_dependency(self) -> None:
        self.add_prompt("api", "20260801_01_First.md",
                        frontmatter="requires:\n  - repo: api\n    notaprompt: x")
        state = resolver.resolve("api", self.data)
        self.assertEqual(state["action"], "blocked")
        self.assertEqual(state.get("reason_code"), "malformed_frontmatter")


class TestSequenceExtraction(WorkspaceCase):
    def test_the_plan_emits_the_configured_alias_spelling(self) -> None:
        self.config_path.write_text(json.dumps({
            "schema": workspace_config.SCHEMA,
            "product": {"id": "example", "name": "Example Product"},
            "data_root": "../workspace-data",
            "repositories": [{"alias": "API", "path": "../example-api"}],
        }, indent=2), encoding="utf-8")
        (self.data / "LLM" / "prompts" / "API").mkdir(parents=True)
        (self.data / "LLM" / "prompts" / "API" / "20260801_01_First.md").write_text(
            "# first\n", encoding="utf-8")
        plan = sequence_plan.build_plan(workspace_config.load(self.config_path))
        self.assertIn("--queue API", plan["serial_command"])
        self.assertNotIn("--queue api ", plan["serial_command"])

    def test_the_plan_orders_a_cross_repository_dependency(self) -> None:
        self.add_prompt("api", "20260801_01_First.md",
                        frontmatter="repo: api\nmutation_targets:\n  - api")
        self.add_prompt("web", "20260801_02_Second.md",
                        frontmatter=("repo: web\nmutation_targets:\n  - web\n"
                                     "requires:\n  - repo: api\n    prompt: 20260801_01_First.md"))
        plan = sequence_plan.build_plan(self.workspace)
        command = plan["serial_command"]
        self.assertLess(command.index("20260801_01_First.md"),
                        command.index("20260801_02_Second.md"))

    def test_a_queue_filter_narrows_the_census_it_reports_too(self) -> None:
        """A one-repository report printed the whole workspace's totals —
        "repositories inspected: 1 / prompts discovered: N / deferred: M" — with
        not one of those M named anywhere below it, because the deferral was an
        artefact of counting rather than a decision about a prompt. A repository
        count and a prompt count that describe different sets cannot both be read
        off one summary."""
        self.add_prompt("api", "20260801_01_First.md")
        self.add_prompt("web", "20260801_01_Other.md")
        self.add_prompt("web", "20260801_02_Another.md")
        plan = sequence_plan.build_plan(self.workspace, ["api"])
        self.assertEqual({entry["app"] for entry in plan["census"]}, {"api"})
        self.assertEqual(len(plan["census"]), 1)
        self.assertEqual(len(plan["repositories"]), 1)
        self.assertNotIn("deferred", {entry["state"] for entry in plan["census"]})
        # The whole-workspace report still counts the whole workspace.
        self.assertEqual(len(sequence_plan.build_plan(self.workspace)["census"]), 3)

    def test_a_cross_repository_prerequisite_is_still_named_when_narrowed(self) -> None:
        """The dependency graph stays workspace-wide even though the census does
        not: the commonest reason a queue will not advance is a prompt in another
        one, and the report has to be able to say so by name."""
        self.add_prompt("web", "20260801_01_Base.md")
        self.add_prompt(
            "api", "20260801_01_Needs.md",
            frontmatter="requires:\n  - repo: web\n    prompt: 20260801_01_Base.md",
        )
        plan = sequence_plan.build_plan(self.workspace, ["api"])
        self.assertEqual(plan["steps"], [])
        entry = next(item for item in plan["excluded"] if item["prompt"] == "20260801_01_Needs.md")
        self.assertEqual(entry["state"], "deferred")
        self.assertIn("web", entry["reason"])

    def test_an_empty_queue_says_so_rather_than_unschedulable(self) -> None:
        """Two facts that call for opposite reactions: finished work, versus a
        queue full of prompts none of which can start."""
        self.add_prompt("api", "20260801_01_First.md")
        self.complete("api", "20260801_01_First.md")
        report = sequence_plan.render_human(sequence_plan.build_plan(self.workspace, ["api"]))
        self.assertIn("every queue inspected is empty", report)

        self.add_prompt(
            "api", "20260801_02_Second.md",
            frontmatter="requires:\n  - 20260801_99_Missing",
        )
        report = sequence_plan.render_human(sequence_plan.build_plan(self.workspace, ["api"]))
        self.assertIn("no queued prompt is currently schedulable", report)

    def test_a_finished_but_unmoved_prompt_is_invalid_whatever_the_scope(self) -> None:
        """The completed-but-not-moved guard is a statement about ONE file's own
        state, so nothing another queue does may mask it. It used to sit below the
        prerequisite chase, which gave one file two diagnoses depending on which
        queues were in scope: a narrowed plan reported a prerequisite wait, and a
        whole-workspace plan that scheduled that prerequisite reported the truth."""
        self.add_prompt("web", "20260801_01_Base.md")
        self.add_prompt(
            "api", "20260801_01_Done.md",
            frontmatter="requires:\n  - repo: web\n    prompt: 20260801_01_Base.md",
        )
        state = agent_task.resolve_state(
            self.repos["api"], "20260801_01_Done.md", workspace=self.workspace)
        agent_task.checkpoint(state, "complete")

        result = resolver.resolve("api", self.data)
        self.assertEqual(result.get("reason_code"), "completed_but_not_moved")
        self.assertNotIn("requires", result["reason"])
        for queues in (None, ["api"]):
            plan = sequence_plan.build_plan(self.workspace, queues)
            entry = next(
                item for item in plan["excluded"] if item["prompt"] == "20260801_01_Done.md"
            )
            self.assertEqual(entry["state"], "invalid", queues)
            self.assertIn("never moved", entry["reason"])

    def test_an_unmet_prerequisite_still_blocks_an_unfinished_prompt(self) -> None:
        """The reorder must not have turned the prerequisite chase off for the
        ordinary case: no complete handoff, so the chase is still the answer."""
        self.add_prompt(
            "api", "20260801_01_Needs.md",
            frontmatter="requires:\n  - 20260801_99_Missing",
        )
        result = resolver.resolve("api", self.data)
        self.assertEqual(result["action"], "blocked")
        self.assertNotIn("reason_code", result)
        self.assertIn("requires", result["reason"])

    def test_the_plan_writes_nothing(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        before = sorted(str(p) for p in self.data.rglob("*"))
        sequence_plan.build_plan(self.workspace)
        self.assertEqual(sorted(str(p) for p in self.data.rglob("*")), before)


class TestCheckpointing(WorkspaceCase):
    def test_a_handoff_pins_the_prompt_and_lands_in_the_alias_folder(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        state = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        written = agent_task.checkpoint(state, "complete")
        self.assertEqual(written.parent, self.data / "LLM" / "handoffs" / "api")
        text = written.read_text(encoding="utf-8")
        self.assertIn("repository_alias: api", text)
        self.assertIn("prompt_path: LLM/prompts/api/20260801_01_First.md", text)
        self.assertIn("status: complete", text)

    def test_a_repository_outside_the_configuration_is_refused(self) -> None:
        stranger = self.father / "not-configured"
        stranger.mkdir()
        with self.assertRaises(agent_task.TaskError) as caught:
            agent_task.resolve_state(stranger, workspace=self.workspace)
        self.assertIn("not a repository configured in", str(caught.exception))

    def test_the_prompt_path_survives_the_move_into_done(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        state = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        agent_task.checkpoint(state, "complete")
        self.complete("api", "20260801_01_First.md")
        moved = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        self.assertEqual(
            agent_task.canonical_prompt_path(moved.latest_prompt, self.data),
            "LLM/prompts/api/20260801_01_First.md",
        )

    def test_a_completed_prompt_still_in_the_queue_is_reported_not_re_run(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        state = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        agent_task.checkpoint(state, "complete")
        result = resolver.resolve("api", self.data)
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result.get("reason_code"), "completed_but_not_moved")

    def test_a_blocked_handoff_carries_its_impact_statement(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        state = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        written = agent_task.checkpoint(state, "blocked", {
            "severity": "dependent",
            "reason": "missing_fixture",
            "summary": "The fixture this prompt needs does not exist.",
            "blocks_prompts": ["20260801_02_Second"],
        })
        block = agent_task.block_metadata(written)
        self.assertEqual(block["severity"], "dependent")
        self.assertEqual(block["blocks_prompts"], ["20260801_02_Second"])


class TestTiming(WorkspaceCase):
    def test_timing_is_recorded_under_the_configured_data_root(self) -> None:
        run_dir = self.data / ".run-sequence" / "testrun"
        execution_time.record_attempt(
            run_dir, "api", "20260801_01_First.md", 1,
            "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z", "complete",
        )
        totals = execution_time.totals(run_dir, "api", "20260801_01_First.md")
        self.assertEqual(totals["execution_seconds"], 300)
        self.assertEqual(totals["execution_attempts"], 1)
        self.assertTrue(str(run_dir).startswith(str(self.data)))

    def test_applying_timing_twice_produces_the_same_number(self) -> None:
        self.add_prompt("api", "20260801_01_First.md")
        state = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        handoff = agent_task.checkpoint(state, "in_progress")
        run_dir = self.data / ".run-sequence" / "testrun"
        execution_time.record_attempt(
            run_dir, "api", "20260801_01_First.md", 1,
            "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z", "complete")
        execution_time.apply(run_dir, "api", "20260801_01_First.md", handoff, True)
        once = execution_time.read_fields(handoff)
        execution_time.apply(run_dir, "api", "20260801_01_First.md", handoff, True)
        self.assertEqual(execution_time.read_fields(handoff), once)
        self.assertEqual(once["execution_seconds"], "300")

    def test_a_checkpoint_preserves_the_runners_measurement(self) -> None:
        """The router rewrites the whole header on every checkpoint. It must carry
        the timing forward rather than erase what only the runner could observe."""
        self.add_prompt("api", "20260801_01_First.md")
        state = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        handoff = agent_task.checkpoint(state, "in_progress")
        run_dir = self.data / ".run-sequence" / "testrun"
        execution_time.record_attempt(
            run_dir, "api", "20260801_01_First.md", 1,
            "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z", "complete")
        execution_time.apply(run_dir, "api", "20260801_01_First.md", handoff, True)
        refreshed = agent_task.resolve_state(
            self.repos["api"], "20260801_01_First.md", workspace=self.workspace)
        agent_task.checkpoint(refreshed, "complete")
        self.assertEqual(execution_time.read_fields(handoff)["execution_seconds"], "300")


if __name__ == "__main__":
    unittest.main()
