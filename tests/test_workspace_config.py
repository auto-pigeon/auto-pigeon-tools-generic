#!/usr/bin/env python3
"""The configuration contract: what workspace.json means and what it refuses.

Every test builds its own throwaway tree under a temporary directory. Nothing
here reads the developer's real repositories, and nothing writes outside its own
`TemporaryDirectory`.
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

import workspace_config  # noqa: E402


def write_config(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def base_payload(**overrides) -> dict:
    payload = {
        "schema": workspace_config.SCHEMA,
        "product": {"id": "example", "name": "Example Product"},
        "data_root": "../workspace-data",
        "repositories": [
            {"alias": "api", "path": "../example-api"},
            {"alias": "web", "path": "../example-web"},
        ],
        "agents": {
            "claude": {"enabled": True, "command": "claude"},
            "codex": {"enabled": True, "command": "codex"},
        },
    }
    payload.update(overrides)
    return payload


class ConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.father = Path(self._tmp.name)
        self.toolkit = self.father / "toolkit"
        self.toolkit.mkdir()
        for name in ("example-api", "example-web"):
            (self.father / name).mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self, payload: dict) -> Path:
        return write_config(self.toolkit / "workspace.json", payload)


class TestPathResolution(ConfigCase):
    def test_relative_paths_resolve_from_the_configuration_file(self) -> None:
        """Not from the current working directory — every runner is invoked from
        somewhere different, and a cwd-relative path would mean a different
        repository per caller."""
        path = self.config(base_payload())
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            workspace = workspace_config.load(path)
        finally:
            os.chdir(cwd)
        self.assertEqual(
            workspace.require("api").path.resolve(),
            (self.father / "example-api").resolve(),
        )

    def test_absolute_paths_are_taken_as_written(self) -> None:
        absolute = self.father / "elsewhere"
        absolute.mkdir()
        path = self.config(base_payload(repositories=[
            {"alias": "api", "path": str(absolute)},
        ]))
        workspace = workspace_config.load(path)
        self.assertEqual(workspace.require("api").path, absolute)

    def test_paths_containing_spaces_survive(self) -> None:
        spaced = self.father / "my worker repo"
        spaced.mkdir()
        path = self.config(base_payload(repositories=[
            {"alias": "worker", "path": "../my worker repo"},
        ]))
        workspace = workspace_config.load(path)
        self.assertTrue(workspace.require("worker").path.is_dir())
        self.assertIn(" ", str(workspace.require("worker").path))

    def test_data_root_resolves_relative_to_the_configuration(self) -> None:
        workspace = workspace_config.load(self.config(base_payload()))
        self.assertEqual(
            workspace.data_root.resolve(),
            (self.father / "workspace-data").resolve(),
        )

    def test_tilde_expands(self) -> None:
        path = self.config(base_payload(data_root="~/some-data-root"))
        workspace = workspace_config.load(path)
        self.assertTrue(str(workspace.data_root).startswith(str(Path.home())))


class TestAliases(ConfigCase):
    def test_lookup_is_case_insensitive_but_the_spelling_is_preserved(self) -> None:
        workspace = workspace_config.load(self.config(base_payload(repositories=[
            {"alias": "API", "path": "../example-api"},
        ])))
        for spelling in ("API", "api", "Api", "aPi"):
            self.assertEqual(workspace.require(spelling).alias, "API")

    def test_duplicate_aliases_fail(self) -> None:
        path = self.config(base_payload(repositories=[
            {"alias": "api", "path": "../example-api"},
            {"alias": "api", "path": "../example-web"},
        ]))
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(path)
        self.assertIn("duplicate repository alias", str(caught.exception))

    def test_aliases_differing_only_in_case_are_duplicates(self) -> None:
        path = self.config(base_payload(repositories=[
            {"alias": "api", "path": "../example-api"},
            {"alias": "API", "path": "../example-web"},
        ]))
        with self.assertRaises(workspace_config.WorkspaceError):
            workspace_config.load(path)

    def test_an_alias_may_not_contain_a_path_separator(self) -> None:
        """An alias also names a queue folder, so a separator in it would write
        outside the queue it claims to be."""
        path = self.config(base_payload(repositories=[
            {"alias": "../escape", "path": "../example-api"},
        ]))
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(path)
        self.assertIn("path separator", str(caught.exception))

    def test_unknown_alias_fails_naming_what_is_configured(self) -> None:
        workspace = workspace_config.load(self.config(base_payload()))
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace.require("nope")
        message = str(caught.exception)
        self.assertIn("unknown repository alias", message)
        self.assertIn("api", message)
        self.assertIn("web", message)


class TestDuplicatePaths(ConfigCase):
    def test_two_aliases_on_one_directory_fail(self) -> None:
        path = self.config(base_payload(repositories=[
            {"alias": "api", "path": "../example-api"},
            {"alias": "backend", "path": "../example-api"},
        ]))
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(path)
        self.assertIn("same worktree", str(caught.exception))

    def test_a_symlink_to_the_same_checkout_is_the_same_worktree(self) -> None:
        """Identity is the RESOLVED path. Two aliases reaching one checkout
        through different names are one repository, and a runner that thought
        otherwise would snapshot one and verify the other."""
        link = self.father / "api-link"
        try:
            link.symlink_to(self.father / "example-api")
        except OSError:
            self.skipTest("this filesystem does not support symlinks")
        path = self.config(base_payload(repositories=[
            {"alias": "api", "path": "../example-api"},
            {"alias": "linked", "path": "../api-link"},
        ]))
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(path)
        self.assertIn("same worktree", str(caught.exception))

    def test_different_spellings_of_one_path_are_the_same_worktree(self) -> None:
        path = self.config(base_payload(repositories=[
            {"alias": "api", "path": "../example-api"},
            {"alias": "same", "path": "../example-web/../example-api"},
        ]))
        with self.assertRaises(workspace_config.WorkspaceError):
            workspace_config.load(path)


class TestMalformedAndMissing(ConfigCase):
    def test_missing_configuration_names_init_sh(self) -> None:
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(self.toolkit / "workspace.json")
        message = str(caught.exception)
        self.assertIn("no workspace configuration", message)
        self.assertIn("./init.sh", message)

    def test_invalid_json_fails_visibly_with_a_position(self) -> None:
        path = self.toolkit / "workspace.json"
        path.write_text('{"product": {"id": "x",}}', encoding="utf-8")
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(path)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_a_missing_product_id_fails(self) -> None:
        payload = base_payload()
        payload["product"] = {}
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(self.config(payload))
        self.assertIn("product.id", str(caught.exception))

    def test_a_missing_data_root_fails(self) -> None:
        payload = base_payload()
        del payload["data_root"]
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(self.config(payload))
        self.assertIn("data_root", str(caught.exception))

    def test_an_unknown_schema_fails_rather_than_being_guessed_at(self) -> None:
        payload = base_payload(schema="something-else/9.9")
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(self.config(payload))
        self.assertIn("does not implement", str(caught.exception))

    def test_a_repository_entry_without_a_path_fails(self) -> None:
        payload = base_payload(repositories=[{"alias": "api"}])
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace_config.load(self.config(payload))
        self.assertIn("path", str(caught.exception))

    def test_a_missing_repository_directory_is_reported_with_its_path(self) -> None:
        payload = base_payload(repositories=[
            {"alias": "gone", "path": "../not-cloned-yet"},
        ])
        workspace = workspace_config.load(self.config(payload))
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace.require("gone")
        message = str(caught.exception)
        self.assertIn("not-cloned-yet", message)
        self.assertIn("does not exist", message)

    def test_nothing_falls_back_to_a_previous_products_layout(self) -> None:
        """A broken configuration must never be answered with a guess. The point
        of the whole exercise: no inherited manifest, no ladder of default data
        directories, no second format."""
        path = self.toolkit / "workspace.json"
        path.write_text("not json at all", encoding="utf-8")
        with self.assertRaises(workspace_config.WorkspaceError):
            workspace_config.load(path)


class TestPrecedence(ConfigCase):
    def test_explicit_argument_beats_the_environment(self) -> None:
        explicit = write_config(self.father / "explicit.json", base_payload(
            product={"id": "explicit", "name": "Explicit"}))
        other = write_config(self.father / "env.json", base_payload(
            product={"id": "from-env", "name": "Env"}))
        previous = os.environ.get(workspace_config.CONFIG_ENV)
        os.environ[workspace_config.CONFIG_ENV] = str(other)
        try:
            self.assertEqual(workspace_config.load(explicit).product_id, "explicit")
        finally:
            if previous is None:
                os.environ.pop(workspace_config.CONFIG_ENV, None)
            else:
                os.environ[workspace_config.CONFIG_ENV] = previous

    def test_the_environment_beats_the_toolkit_root(self) -> None:
        self.config(base_payload(product={"id": "at-root", "name": "Root"}))
        other = write_config(self.father / "env.json", base_payload(
            product={"id": "from-env", "name": "Env"}))
        previous = os.environ.get(workspace_config.CONFIG_ENV)
        os.environ[workspace_config.CONFIG_ENV] = str(other)
        try:
            resolved = workspace_config.config_path(None, start=self.toolkit)
            self.assertEqual(resolved, other)
        finally:
            if previous is None:
                os.environ.pop(workspace_config.CONFIG_ENV, None)
            else:
                os.environ[workspace_config.CONFIG_ENV] = previous


class TestAgents(ConfigCase):
    def test_the_command_is_configuration_not_a_literal(self) -> None:
        workspace = workspace_config.load(self.config(base_payload(agents={
            "claude": {"enabled": True, "command": "/opt/bin/claude-wrapper"},
            "codex": {"enabled": True, "command": "codex"},
        })))
        self.assertEqual(workspace.agent_command("claude"), "/opt/bin/claude-wrapper")
        self.assertEqual(workspace.agent_command("codex"), "codex")

    def test_a_disabled_agent_refuses(self) -> None:
        workspace = workspace_config.load(self.config(base_payload(agents={
            "claude": {"enabled": False, "command": "claude"},
        })))
        with self.assertRaises(workspace_config.WorkspaceError) as caught:
            workspace.agent_command("claude")
        self.assertIn("disabled", str(caught.exception))

    def test_both_agents_are_available_by_default(self) -> None:
        payload = base_payload()
        del payload["agents"]
        workspace = workspace_config.load(self.config(payload))
        self.assertEqual(workspace.agent_command("claude"), "claude")
        self.assertEqual(workspace.agent_command("codex"), "codex")


class TestCanonicalDerivations(ConfigCase):
    def test_the_alias_is_the_queue_and_handoff_folder(self) -> None:
        """ONE derivation. There is no per-repository file that could name a
        different folder, which is what made the source toolkit's two answers
        possible."""
        workspace = workspace_config.load(self.config(base_payload()))
        self.assertEqual(
            workspace.prompt_dir("api"),
            workspace.data_root / "LLM" / "prompts" / "api",
        )
        self.assertEqual(
            workspace.handoff_dir("API"),
            workspace.data_root / "LLM" / "handoffs" / "api",
        )


class TestCommandLine(ConfigCase):
    """The interface the shell runners actually consume."""

    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "workspace_config.py"),
             "--config", str(self.toolkit / "workspace.json"), *args],
            capture_output=True, text=True,
        )

    def test_print_aliases_is_tab_separated_so_spaces_survive(self) -> None:
        spaced = self.father / "my worker"
        spaced.mkdir()
        self.config(base_payload(repositories=[
            {"alias": "worker", "path": "../my worker"},
        ]))
        result = self.run_tool("--print", "aliases")
        self.assertEqual(result.returncode, 0, result.stderr)
        alias, _, path = result.stdout.strip().partition("\t")
        self.assertEqual(alias, "worker")
        self.assertTrue(path.endswith("my worker"))

    def test_unknown_alias_exits_nonzero(self) -> None:
        self.config(base_payload())
        result = self.run_tool("--path", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown repository alias", result.stderr)


if __name__ == "__main__":
    unittest.main()
