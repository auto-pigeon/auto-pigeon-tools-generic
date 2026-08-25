#!/usr/bin/env python3
"""The regression that keeps one product's topology out of a generic toolkit.

WHY THIS EXISTS
---------------
This toolkit was extracted from a working single-product one. Extraction removed
the repository names, the data-root path, the alias table and the launcher hooks
— but extraction is a one-time act and re-entry is a continuous risk: the easiest
way to make a runner work is to hardcode the path that works on your machine.

So the forbidden tokens are checked, by name, on every test run.

THE ALLOWLIST IS NARROW ON PURPOSE
----------------------------------
Two things are legitimately allowed to mention the origin:

  * `bootstrap/` — the prompt that commissioned this repository. It is a
    historical record and changing it would falsify it.
  * documentation and comments that explain where this came from, in the files
    named below and nowhere else.

Executable code is allowed NONE of it. A token inside a comment in a runner is
still a finding: comments become code when somebody copies them.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The topology of the product this was extracted from. Each entry is matched
#: case-insensitively, as a whole word where a word boundary is meaningful.
FORBIDDEN = (
    "/home/bario",
    "/mapper",
    "/mapper-code",
    "auto-pigeon-backend",
    "auto-pigeon-collaboration",
    "auto-pigeon-extractor",
    "auto-pigeon-gallery",
    "auto-pigeon-libraries",
    "auto-pigeon-companion",
    "auto-pigeon-launcher",
    "ai-mapcopilot",
    "MAPPER_ROOT",
    "AULIBS",
    "launch-aup",
    "teardown-aup",
    ".agent-repo.json",
    "auto-pigeon-workspace.json",
)

#: Short aliases, matched as whole words so that ordinary English survives:
#: "aup" must be a finding, while the "AUB" inside some longer identifier is not
#: what this is looking for.
FORBIDDEN_WORDS = ("AUP", "AUB", "AUC", "AUE", "AUG", "AUCOM", "AUL", "AIM", "PB", "COL")

#: `auto-pigeon` on its own would match this repository's own brand
#: (`auto-pigeon-toolkit`), which the commissioning prompt explicitly permits. So
#: the bare product name is matched only where it is NOT followed by `-toolkit`
#: or `-tools`.
BARE_PRODUCT = re.compile(r"auto-pigeon(?!-toolkit|-tools)", re.IGNORECASE)

#: Everything that can execute. These get the strict rule.
EXECUTABLE = (
    "init.sh",
    "run-agent.sh",
    "run-sequence.sh",
    "scripts/agent_task.py",
    "scripts/branch_policy.py",
    "scripts/dirty_baseline.py",
    "scripts/execution_history.py",
    "scripts/execution_time.py",
    "scripts/prompt_frontmatter.py",
    "scripts/resolve_next_prompt.py",
    "scripts/sequence_plan.py",
    "scripts/stream_progress.py",
    "scripts/workspace_config.py",
    "scripts/workspace_init.py",
)

#: Files allowed to name the origin, and why. Documentation explaining what this
#: was extracted from is useful; a runner that resolves a path through it is not.
#:
#:   README.md / AGENTS.md / WORKFLOW.md   explain where this came from and what
#:                                         was deliberately left behind.
#:   LICENSE                               names the copyright holder. That is an
#:                                         ownership fact, not a topology, and
#:                                         editing it to satisfy a grep would be
#:                                         the wrong kind of tidy.
#:   this file                             has to spell the tokens to look for them.
DOCUMENTED_ORIGIN = (
    "README.md",
    "AGENTS.md",
    "WORKFLOW.md",
    "LICENSE",
    "tests/test_no_product_topology.py",
)

#: Directories excluded wholesale.
EXCLUDED_DIRS = {".git", "graft", "__pycache__", "bootstrap", ".claude"}


def repository_files() -> list[Path]:
    found: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if EXCLUDED_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if path.suffix in (".pyc", ".png", ".jpg", ".gz"):
            continue
        found.append(path)
    return found


def findings(text: str) -> list[str]:
    hits: list[str] = []
    lowered = text.lower()
    for token in FORBIDDEN:
        if token.lower() in lowered:
            hits.append(token)
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            hits.append(word)
    if BARE_PRODUCT.search(text):
        hits.append("auto-pigeon (bare product name)")
    return hits


class TestNoProductTopology(unittest.TestCase):
    def test_executable_code_names_no_product_topology(self) -> None:
        """Not in code, not in a string, not in a comment."""
        problems: list[str] = []
        for relative in EXECUTABLE:
            path = ROOT / relative
            self.assertTrue(path.is_file(), f"{relative} is missing")
            hits = findings(path.read_text(encoding="utf-8"))
            if hits:
                problems.append(f"{relative}: {', '.join(sorted(set(hits)))}")
        self.assertEqual(problems, [], "product topology re-entered executable code:\n"
                         + "\n".join(problems))

    def test_only_allowlisted_files_may_name_the_origin(self) -> None:
        problems: list[str] = []
        for path in repository_files():
            relative = path.relative_to(ROOT).as_posix()
            if relative in DOCUMENTED_ORIGIN:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits = findings(text)
            if hits:
                problems.append(f"{relative}: {', '.join(sorted(set(hits)))}")
        self.assertEqual(problems, [], "product topology outside the allowlist:\n"
                         + "\n".join(problems))

    def test_no_hardcoded_absolute_home_path(self) -> None:
        """A developer's own path is the single most common way this regresses."""
        problems: list[str] = []
        pattern = re.compile(r"/(?:home|Users)/[a-z][a-z0-9_.-]*", re.IGNORECASE)
        for path in repository_files():
            relative = path.relative_to(ROOT).as_posix()
            if relative == "tests/test_no_product_topology.py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in pattern.finditer(text):
                problems.append(f"{relative}: {match.group(0)}")
        self.assertEqual(problems, [], "an absolute home path is hardcoded:\n"
                         + "\n".join(problems))

    def test_the_bootstrap_prompt_is_preserved_and_excluded(self) -> None:
        """It names the origin throughout — that is what makes it the record."""
        prompts = list((ROOT / "bootstrap").glob("*.md"))
        self.assertTrue(prompts, "the commissioning prompt must be kept in bootstrap/")
        self.assertIn("bootstrap", EXCLUDED_DIRS)

    def test_every_shell_script_parses(self) -> None:
        """`bash -n` on everything, so a broken script cannot pass by not running."""
        import subprocess
        for path in sorted(ROOT.rglob("*.sh")):
            if EXCLUDED_DIRS & set(path.relative_to(ROOT).parts):
                continue
            result = subprocess.run(["bash", "-n", str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                             f"{path.relative_to(ROOT)}: {result.stderr}")

    def test_no_module_imports_the_retired_topology_helper(self) -> None:
        """`workspace_paths` was the source toolkit's manifest reader. Anything
        importing it would be reading a manifest that does not exist here."""
        for path in sorted((ROOT / "scripts").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("import workspace_paths", text, path.name)


if __name__ == "__main__":
    unittest.main()
