#!/usr/bin/env python3
"""Read-only dependency-aware planner behind `run-sequence.sh --extract-sequence`.

WHAT THIS IS FOR
----------------
An operator looking at seven queue folders cannot see, without reading every
frontmatter block, which prompt may run now, which is waiting on something in
another repository, and which two could honestly run at the same time. This
answers that question and prints the answer as commands they can paste.

IT NEVER MUTATES ANYTHING. No agent is started, no prompt is moved, no handoff
is written, no repository is touched and no queue state changes. The only thing
it opens for writing is nothing at all.

ONE SOURCE OF TRUTH, AND IT IS THE EXECUTOR'S
---------------------------------------------
The temptation here is to re-derive "what is outstanding" from the frontmatter,
because that is easy. It is also how a planner comes to disagree with the runner
about which prompt is next, which is worse than having no planner: it is a
confident wrong answer.

So this module does not decide selection at all. It calls
`resolve_next_prompt.resolve()` — the same function `run-sequence.sh` calls, in
the same order, with the same `--skip` semantics — and walks the plan forward by
telling that function to *assume* the steps already emitted are in `done/`
(`assume_done`). The queue folder on disk is never touched; the overlay lives in
a set. Whatever the resolver would pick, this prints, including the completed-
but-not-moved guard and the recursive prerequisite chase.

The classification, dependency-cycle report, duplicate detection and
parallel-lane analysis on top of that are this module's own — but they are
REPORTING over the resolver's answer, never a second opinion about it.

WHAT `--queue` NARROWS, AND WHAT IT DELIBERATELY DOES NOT
--------------------------------------------------------
The dependency graph is always workspace-wide: a prompt waiting on another
repository is the commonest reason a queue will not advance, and a planner that
could not see into that queue would have to report "unknown" instead of the
answer. Everything the report COUNTS or PRINTS, though, is narrowed to the
queues asked about, plus whatever they transitively require. A state summary is
a description of what was inspected; "repositories inspected: 1" over a census
of the whole workspace is not a more generous answer, it is a contradictory one.

WHAT "PARALLEL-SAFE" MEANS HERE
-------------------------------
Two prompts may go in different lanes only when every one of these holds:

  * neither depends on the other, directly or transitively;
  * their effective mutation targets are disjoint;
  * both actually DECLARE their mutation targets — an undeclared scope is
    treated as "could touch anything", never as "touches nothing";
  * they are not in the same prompt/handoff directory (two agents writing into
    one queue folder is a mutation conflict like any other).

Anything unproven is serial. A wrongly-parallel pair corrupts a repository; a
wrongly-serial pair costs some wall clock.

Usage:
    sequence_plan.py [--config workspace.json] [--queue ALIAS]... \\
                     [--format human|shell|json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prompt_frontmatter as fm  # noqa: E402
import resolve_next_prompt as resolver  # noqa: E402
import workspace_config  # noqa: E402

DONE_DIRNAME = resolver.DONE_DIRNAME
BLOCKED_DIRNAME = resolver.BLOCKED_DIRNAME

# Every state a discovered prompt can be in. The set is closed on purpose: a
# reader of the JSON should never meet a value this docstring does not explain.
STATES = (
    "complete",
    "partial",
    "in_progress",
    "blocked",
    "ready",
    "deferred",
    "invalid",
    "stale_or_duplicate",
)

# A prompt's TASK ID, for duplicate detection — and it deliberately does NOT
# match a bare `<date>_<number>`.
#
# `20260823_94_Collaboration-Polish` and `20260823_94_Something-Else` in two
# different repositories are not the same task: the number is a PER-REPOSITORY
# sequence, and every queue restarts it. Treating those as duplicates would
# report most of a busy workspace as ambiguous and schedule none of it.
#
# What IS workspace-wide is an alphabetic tag — `OBS01`, `AUT_05`, `PREPROD-05`,
# `ABC-06`. Those name one task, so the same one in two queues is a real
# cross-queue copy. Everything else has no task id and collides only on an
# identical FILENAME, which is unambiguous on its own.
TASK_ID_RE = re.compile(
    r"\A(?P<id>"
    r"\d{8}(?:T\d{6}Z)?_[A-Za-z][A-Za-z0-9-]*(?:_\d+[A-Za-z]?)?"  # 20260823_OBS01, 20260823_AUT_05
    r"|[A-Z]{2,8}-\d+[A-Z]?"                                       # ABC-06
    r")"
)


# ---------------------------------------------------------------------------
# Frontmatter: mutation targets
# ---------------------------------------------------------------------------
#: Inside a mapping list item, the key that names the repository.
MUTATION_TARGET_KEYS = ("repo", "repository", "directory", "path")


def parse_mutation_targets(path: Path) -> list[str] | None:
    """Declared mutation targets, or None when the prompt declares none.

    None is NOT the empty list and the difference decides parallelism: an
    undeclared scope means "unknown", which is treated conservatively, while an
    explicit empty list would mean "mutates nothing", which no prompt is.

    Every accepted spelling — flow list, `-` bullets, the `*` bullets a Markdown
    editor leaves behind, a bare scalar, a `repo:` mapping item — is decoded by
    `prompt_frontmatter`, the one decoder. This function used to carry its own,
    which is how it came to be RIGHT about one prompt's mutation targets while
    `extract_requires` was wrong about the `requires:` block eight lines below
    them. Two hand parsers over one file is one opinion too many.
    """
    if not path.is_file():
        return None
    items = fm.parse_prompt(path).field_list("mutation_targets")
    if items is None:
        return None
    targets: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = next((str(item[key]) for key in MUTATION_TARGET_KEYS if item.get(key)), "")
        else:
            value = str(item)
        if value.strip():
            targets.append(value.strip())
    return targets or None


# ---------------------------------------------------------------------------
# Repository discovery — the same rule run-sequence.sh's drain uses
# ---------------------------------------------------------------------------
def discover_repositories(workspace: workspace_config.Workspace) -> list[dict]:
    """Every configured repository that can actually be worked, in config order.

    A checkout on disk and a queue folder for its alias under the data root.
    Identical to run-sequence.sh's own discovery, deliberately: a planner that
    saw a repository the drain skips would advertise commands the drain refuses.
    """
    found: list[dict] = []
    for repo in workspace.repositories:
        if not repo.exists:
            continue
        if not (workspace.data_root / "LLM" / "prompts" / repo.alias).is_dir():
            continue
        found.append(
            {
                "alias": repo.alias,
                "directory": repo.alias,
                "repo_root": repo.path,
                "prompt_directory": repo.alias,
                "handoff_directory": repo.alias,
            }
        )
    return found


# ---------------------------------------------------------------------------
# The prompt census — every prompt in every queue, wherever it sits
# ---------------------------------------------------------------------------
def _prompt_records(data_root: Path, repositories: list[dict]) -> dict[tuple[str, str], dict]:
    records: dict[tuple[str, str], dict] = {}
    for repo in repositories:
        app = repo["prompt_directory"]
        queue_dir = data_root / "LLM" / "prompts" / app
        handoff_dir = data_root / "LLM" / "handoffs" / repo["handoff_directory"]
        handoffs = resolver._load_handoffs(handoff_dir)
        for location, directory in (
            ("queue", queue_dir),
            (DONE_DIRNAME, queue_dir / DONE_DIRNAME),
            (BLOCKED_DIRNAME, queue_dir / BLOCKED_DIRNAME),
        ):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                if not path.is_file():
                    continue
                handoff = handoffs.get(path.stem)
                task_match = TASK_ID_RE.match(path.stem)
                # A prompt the decoder refuses is RECORDED as unreadable and
                # carried through the plan, never dropped and never given an
                # empty dependency set. `plan()` refuses to emit a sequence
                # while one is present: a planned order computed from edges
                # nobody could read is exactly the confident wrong answer an
                # operator pastes into a terminal.
                malformed: str | None = None
                try:
                    requires = sorted(
                        {
                            (requirement.app or app, requirement.stem)
                            for requirement in resolver.extract_requires(path)
                        }
                    )
                    mutation_targets = parse_mutation_targets(path)
                except fm.FrontmatterError as error:
                    requires, mutation_targets, malformed = [], None, str(error)
                records[(app, path.stem)] = {
                    "app": app,
                    "alias": repo["alias"],
                    "prompt": path.name,
                    "stem": path.stem,
                    "path": path,
                    "location": location,
                    "task_id": task_match.group("id") if task_match else None,
                    "requires": requires,
                    "malformed": malformed,
                    "mutation_targets": mutation_targets,
                    "handoff_status": handoff.status if handoff else None,
                    "handoff_path": str(handoff.path) if handoff else None,
                    "handoff_pins_this": bool(
                        handoff
                        and handoff.prompt_path
                        and Path(handoff.prompt_path).name == path.name
                    ),
                }
    return records


def _find_cycles(records: dict[tuple[str, str], dict]) -> list[list[str]]:
    """Every dependency cycle, each reported IN FULL.

    "A cycle exists" is not a usable report — the operator has to be told which
    prompts to break it between, so each cycle is printed as the whole ring,
    closed back onto its first member.

    Depth-first with a three-colour marking, over an explicitly bounded stack.
    The graph is one workspace's prompts, so this is small; the explicit stack is
    only so that a pathological `requires:` chain cannot take the planner down
    with a RecursionError.
    """
    def label(key: tuple[str, str]) -> str:
        return f"{key[0]}/{key[1]}"

    def edges(key: tuple[str, str]) -> list[tuple[str, str]]:
        return [
            tuple(requirement)
            for requirement in records.get(key, {}).get("requires", [])
            if tuple(requirement) in records
        ]

    cycles: list[list[str]] = []
    known: set[tuple[str, ...]] = set()
    finished: set[tuple[str, str]] = set()

    for start in sorted(records):
        if start in finished:
            continue
        # Each frame is (node, index of the next edge to walk).
        stack: list[list] = [[start, 0]]
        path: list[tuple[str, str]] = [start]
        on_path: set[tuple[str, str]] = {start}
        while stack:
            frame = stack[-1]
            node, index = frame[0], frame[1]
            children = edges(node)
            if index >= len(children):
                finished.add(node)
                on_path.discard(node)
                path.pop()
                stack.pop()
                continue
            frame[1] = index + 1
            child = children[index]
            if child in on_path:
                ring = path[path.index(child):]
                # Rotate to a canonical start so the same ring found from two
                # entry points is reported once.
                pivot = ring.index(min(ring))
                ring = ring[pivot:] + ring[:pivot]
                fingerprint = tuple(label(item) for item in ring)
                if fingerprint not in known:
                    known.add(fingerprint)
                    cycles.append([*fingerprint, fingerprint[0]])
                continue
            if child in finished:
                continue
            stack.append([child, 0])
            path.append(child)
            on_path.add(child)
    return sorted(cycles)


# ---------------------------------------------------------------------------
# The plan itself — the resolver, driven forward over a virtual done/
# ---------------------------------------------------------------------------
def build_plan(workspace: workspace_config.Workspace, wanted: list[str] | None = None) -> dict:
    data_root = workspace.data_root
    repositories = discover_repositories(workspace)
    if wanted:
        # An ALIAS or a DIRECTORY, either way. run-sequence.sh resolves its own
        # alias table to a directory before calling here, but this module is also
        # run directly, and a filter that silently matched nothing would report an
        # empty plan as though the queue were empty.
        wanted_lower = {value.lower() for value in wanted}
        selected = [
            repo
            for repo in repositories
            if repo["alias"].lower() in wanted_lower
            or repo["directory"].lower() in wanted_lower
            or repo["prompt_directory"].lower() in wanted_lower
        ]
        matched = {
            value
            for repo in selected
            for value in (
                repo["alias"].lower(),
                repo["directory"].lower(),
                repo["prompt_directory"].lower(),
            )
        }
        unknown = sorted(wanted_lower - matched)
        if unknown:
            raise workspace_config.WorkspaceError(
                "no repository in this workspace matches --queue "
                + ", ".join(unknown)
                + ". Known: "
                + ", ".join(sorted(repo["alias"] for repo in repositories))
            )
    else:
        selected = repositories

    # The RECORDS cover EVERY discovered repository even when --queue narrows the
    # plan, because a cross-repository prerequisite in a queue nobody asked about
    # is exactly the fact that explains why a scheduled prompt is missing.
    #
    # The CENSUS is a different thing and must not inherit that reach. It is the
    # report of what was INSPECTED, and a one-repository report printing the whole
    # workspace's totals — "repositories inspected: 1 / prompts discovered: 493 /
    # deferred: 15" — was not a bigger answer, it was a wrong one: the 493 were
    # every queue's, and the 15 "deferred" were prompts in queues nobody had asked
    # about and that this plan had never considered — none of them named anywhere
    # in the report, because the deferral was an artefact of counting rather than
    # a fact about a prompt. A repository count and a prompt count that describe
    # different sets cannot both be read off one summary. So: records
    # workspace-wide, census scoped.
    records = _prompt_records(data_root, repositories)
    selected_apps = {repo["prompt_directory"] for repo in selected}
    # Everything the scoped plan can be AFFECTED by: the queues asked about, plus
    # whatever they require, transitively and across repositories. It is what
    # decides which cycles and which unreadable-frontmatter faults are worth
    # printing here — a cycle wholly inside a queue this report was not asked
    # about cannot change any order below it, while one a scheduled prompt
    # depends on is the whole explanation for why that prompt is missing.
    scope = _scope_keys(records, selected_apps)
    cycles = _find_cycles(records)
    in_cycle: set[tuple[str, str]] = set()
    for cycle in cycles:
        for label in cycle:
            app, _, stem = label.partition("/")
            in_cycle.add((app, stem))

    # --- duplicates: the same task id, or the same filename, in two places ---
    by_task: dict[str, list[tuple[str, str]]] = {}
    by_name: dict[str, list[tuple[str, str]]] = {}
    for key, record in records.items():
        if record["task_id"]:
            by_task.setdefault(record["task_id"], []).append(key)
        by_name.setdefault(record["prompt"], []).append(key)
    duplicates: dict[tuple[str, str], str] = {}

    def where(key: tuple[str, str]) -> str:
        return "%s/%s" % (records[key]["app"], records[key]["prompt"])

    for group in list(by_task.values()) + list(by_name.values()):
        if len(group) < 2:
            continue
        # PROVENANCE decides which copy is canonical, where there is any: a
        # handoff that pins a copy by name is the only evidence in the tree that
        # somebody actually worked THAT file. With no such evidence, neither copy
        # is guessed at — both are surfaced, which is what §2.3 asks for.
        canonical = [key for key in group if records[key]["handoff_pins_this"]]
        canonical_labels = ", ".join(sorted(where(key) for key in canonical))
        for key in group:
            others = ", ".join(sorted(where(other) for other in group if other != key))
            if canonical and key not in canonical:
                duplicates[key] = (
                    "a copy of the same task; the canonical copy is the one its "
                    f"handoff pins ({canonical_labels}). Shares identity with {others}"
                )
            elif not canonical:
                duplicates[key] = (
                    f"duplicate task identity with {others}; no handoff pins any "
                    "copy, so which is canonical cannot be decided here"
                )

    # --- walk the resolver forward -------------------------------------------
    assume_done: set[tuple[str, str]] = {
        key for key, record in records.items() if record["location"] == DONE_DIRNAME
    }
    skip: dict[str, set[str]] = {repo["alias"]: set() for repo in selected}
    steps: list[dict] = []
    excluded: list[dict] = []
    excluded_keys: set[tuple[str, str]] = set()
    repo_errors: list[dict] = []
    drained: set[str] = set()

    def exclude(app: str, stem: str, prompt: str, state: str, reason: str) -> None:
        key = (app, stem)
        if key in excluded_keys:
            return
        excluded_keys.add(key)
        excluded.append(
            {"app": app, "prompt": prompt, "stem": stem, "state": state, "reason": reason}
        )

    # The same two-level shape run-sequence.sh drains with: repositories in
    # configuration order, each taken as far as it can go, then another pass while a
    # completion in this plan could have satisfied a cross-repository
    # prerequisite. Bounded by the number of prompts, so a pathological queue
    # cannot spin here.
    max_passes = max(1, len(records) + 1)
    for _ in range(max_passes):
        progressed = False
        for repo in selected:
            if repo["alias"] in drained:
                continue
            while True:
                state = resolver.resolve(
                    repo["alias"],
                    data_root,
                    skip=set(skip[repo["alias"]]),
                    assume_done=set(assume_done),
                )
                action = state.get("action")
                if action == "idle":
                    drained.add(repo["alias"])
                    break
                if action == "error":
                    repo_errors.append(
                        {"alias": repo["alias"], "reason": state.get("reason", "")}
                    )
                    drained.add(repo["alias"])
                    break
                if action == "blocked":
                    candidate = state.get("candidate")
                    if not candidate:
                        repo_errors.append(
                            {"alias": repo["alias"], "reason": state.get("reason", "")}
                        )
                        drained.add(repo["alias"])
                        break
                    stem = Path(candidate).stem
                    skip[repo["alias"]].add(stem)
                    key = (repo["prompt_directory"], stem)
                    if state.get("reason_code") == "completed_but_not_moved":
                        exclude(
                            repo["prompt_directory"], stem, candidate, "invalid",
                            state.get("reason", ""),
                        )
                    elif key in in_cycle:
                        exclude(
                            repo["prompt_directory"], stem, candidate, "invalid",
                            "it is part of a dependency cycle (printed in full above)",
                        )
                    elif "no prompt with that name exists" in state.get("reason", ""):
                        exclude(
                            repo["prompt_directory"], stem, candidate, "invalid",
                            state.get("reason", ""),
                        )
                    else:
                        exclude(
                            repo["prompt_directory"], stem, candidate, "deferred",
                            state.get("reason", ""),
                        )
                    continue

                prompt_path = Path(state["prompt_path"])
                stem = prompt_path.stem
                key = (repo["prompt_directory"], stem)
                if key in duplicates:
                    skip[repo["alias"]].add(stem)
                    exclude(
                        repo["prompt_directory"], stem, prompt_path.name,
                        "stale_or_duplicate", duplicates[key],
                    )
                    continue
                record = records.get(key, {})
                steps.append(
                    {
                        "alias": repo["alias"],
                        "app": repo["prompt_directory"],
                        "prompt": prompt_path.name,
                        "stem": stem,
                        "resume": bool(state.get("resume")),
                        "resume_status": state.get("resume_status"),
                        "requires": record.get("requires", []),
                        "mutation_targets": record.get("mutation_targets"),
                        "state": (state.get("resume_status") or "ready")
                        if state.get("resume")
                        else "ready",
                    }
                )
                assume_done.add(key)
                progressed = True
        if not progressed:
            break
        # Another pass can only buy something when a repository was closed early
        # by a prerequisite that this plan has since scheduled.
        retryable = {
            alias
            for alias in drained
            if any(
                entry["state"] == "deferred" and entry["app"] == _app_of(selected, alias)
                for entry in excluded
            )
        }
        if not retryable:
            break
        for alias in retryable:
            drained.discard(alias)
            skip[alias] = {
                entry["stem"]
                for entry in excluded
                if entry["app"] == _app_of(selected, alias) and entry["state"] != "deferred"
            }
        excluded = [
            entry
            for entry in excluded
            if not (entry["state"] == "deferred" and entry["app"] in
                    {_app_of(selected, alias) for alias in retryable})
        ]
        excluded_keys = {(entry["app"], entry["stem"]) for entry in excluded}

    scheduled_keys = {(step["app"], step["stem"]) for step in steps}
    census = _census(records, scheduled_keys, excluded, duplicates, in_cycle, selected_apps)
    lanes = _phases(steps, records)

    # `in_cycle` above stays computed from EVERY cycle — a scheduled prompt caught
    # in a cross-repository ring is invalid whatever the scope. Only what gets
    # PRINTED is narrowed.
    cycles = [
        cycle for cycle in cycles if any(_cycle_key(label) in scope for label in cycle)
    ]

    # UNREADABLE FRONTMATTER IS REPORTED, NEVER PLANNED AROUND. A prompt whose
    # dependency block the canonical decoder refused has unknown edges, and a
    # sequence computed as if it had none is precisely the confident wrong
    # answer `--extract-sequence` exists to avoid producing.
    malformed = sorted(
        (
            {
                "app": record["app"],
                "prompt": record["prompt"],
                "stem": record["stem"],
                "error": record["malformed"],
            }
            for key, record in records.items()
            if record.get("malformed") and key in scope
        ),
        key=lambda entry: (entry["app"], entry["prompt"]),
    )

    return {
        "schema": "run-sequence.plan/1",
        "malformed": malformed,
        "repositories": [
            {"alias": repo["alias"], "app": repo["prompt_directory"]} for repo in selected
        ],
        "steps": steps,
        "excluded": sorted(excluded, key=lambda entry: (entry["app"], entry["prompt"])),
        "repository_errors": sorted(repo_errors, key=lambda entry: entry["alias"]),
        "cycles": cycles,
        "census": census,
        "phases": lanes,
        "serial_command": render_serial(steps),
        "parallel_command": render_parallel(lanes),
    }


def _cycle_key(label: str) -> tuple[str, str]:
    """`app/stem` back into the key `records` is indexed by."""
    app, _, stem = label.partition("/")
    return (app, stem)


def _scope_keys(
    records: dict[tuple[str, str], dict], selected_apps: set[str]
) -> set[tuple[str, str]]:
    """Every prompt a report narrowed to `selected_apps` can be affected by.

    The selected queues themselves, plus the transitive closure of what they
    require — which is how a fault in a queue nobody asked about still gets
    named when it is the reason a scheduled prompt cannot run. Anything outside
    it is not this report's business: printing it is noise, and counting it is
    the bug this exists to stop.
    """
    scope = {key for key, record in records.items() if record["app"] in selected_apps}
    frontier = list(scope)
    while frontier:
        node = frontier.pop()
        for requirement in records.get(node, {}).get("requires", []):
            child = tuple(requirement)
            if child in records and child not in scope:
                scope.add(child)
                frontier.append(child)
    return scope


def _app_of(repositories: list[dict], alias: str) -> str:
    for repo in repositories:
        if repo["alias"] == alias:
            return repo["prompt_directory"]
    return ""


def _census(
    records: dict[tuple[str, str], dict],
    scheduled: set[tuple[str, str]],
    excluded: list[dict],
    duplicates: dict[tuple[str, str], str],
    in_cycle: set[tuple[str, str]],
    selected_apps: set[str],
) -> list[dict]:
    """Every prompt in the queues this report was ASKED ABOUT, one state each.

    `selected_apps` is what `--queue` narrowed to, and the census honours it even
    though `records` deliberately does not: a state summary is a description of
    what was inspected, so a prompt in a queue this report never planned is not
    "deferred" — it is out of scope, and has no state here at all.
    """
    excluded_state = {(entry["app"], entry["stem"]): entry for entry in excluded}
    out: list[dict] = []
    for key in sorted(records):
        record = records[key]
        if record["app"] not in selected_apps:
            continue
        if record["location"] == DONE_DIRNAME:
            state, reason = "complete", "in done/"
            if record["handoff_status"] not in (None, "complete"):
                reason = f"in done/ (handoff status: {record['handoff_status']})"
        elif record["location"] == BLOCKED_DIRNAME:
            state, reason = "blocked", "parked in blocked/"
        elif key in excluded_state:
            state = excluded_state[key]["state"]
            reason = excluded_state[key]["reason"]
        elif key in duplicates:
            state, reason = "stale_or_duplicate", duplicates[key]
        elif key in in_cycle:
            state, reason = "invalid", "part of a dependency cycle"
        elif key in scheduled:
            status = record["handoff_status"]
            if status in ("partial", "in_progress"):
                state, reason = status, f"scheduled as a resume ({status} handoff)"
            else:
                state, reason = "ready", "scheduled"
        else:
            state, reason = "deferred", "not reached by this plan"
        out.append(
            {
                "app": record["app"],
                "alias": record["alias"],
                "prompt": record["prompt"],
                "state": state,
                "reason": reason,
                "location": record["location"],
                "handoff_status": record["handoff_status"],
                "mutation_targets": record["mutation_targets"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Parallelism
# ---------------------------------------------------------------------------
def _effective_targets(step: dict) -> set[str] | None:
    """The repositories a step can write, or None when that is not knowable.

    A prompt always mutates its own repository and its own prompt/handoff
    folder, whatever it declares. None means the declaration is missing, which
    is "could be anything" and never "nothing".
    """
    declared = step.get("mutation_targets")
    if declared is None:
        return None
    return {step["app"], *declared}


def _conflict(a: dict, b: dict, depends: set[tuple[str, str]]) -> str | None:
    key_a = (a["app"], a["stem"])
    key_b = (b["app"], b["stem"])
    if (key_a, key_b) in depends or (key_b, key_a) in depends:
        return "one depends on the other"
    if a["app"] == b["app"]:
        return f"both write the {a['app']} prompt and handoff folders"
    targets_a = _effective_targets(a)
    targets_b = _effective_targets(b)
    if targets_a is None or targets_b is None:
        missing = a["prompt"] if targets_a is None else b["prompt"]
        return f"{missing} declares no mutation_targets, so its scope is unknown"
    overlap = targets_a & targets_b
    if overlap:
        return f"overlapping mutation targets: {', '.join(sorted(overlap))}"
    return None


def _phases(steps: list[dict], records: dict[tuple[str, str], dict]) -> list[dict]:
    """The plan cut into barriers, each barrier cut into proven-safe lanes."""
    if not steps:
        return []
    index = {(step["app"], step["stem"]): position for position, step in enumerate(steps)}

    # Transitive dependency closure, restricted to the scheduled set.
    depends: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    direct: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for step in steps:
        key = (step["app"], step["stem"])
        direct[key] = {
            tuple(requirement)
            for requirement in step.get("requires", [])
            if tuple(requirement) in index
        }
    for key in index:
        frontier = list(direct.get(key, ()))
        seen: set[tuple[str, str]] = set()
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            depends.add((key, node))
            frontier.extend(direct.get(node, ()))

    depth: dict[tuple[str, str], int] = {}

    def compute(key: tuple[str, str]) -> int:
        if key in depth:
            return depth[key]
        depth[key] = 0  # cycles cannot reach here — they are never scheduled
        value = 0
        for requirement in direct.get(key, ()):
            value = max(value, compute(requirement) + 1)
        depth[key] = value
        return value

    for key in index:
        compute(key)

    phases: list[dict] = []
    for level in sorted(set(depth.values())):
        members = sorted(
            (step for step in steps if depth[(step["app"], step["stem"])] == level),
            key=lambda step: index[(step["app"], step["stem"])],
        )
        # Union-find over the conflict relation: anything that cannot be proven
        # independent ends up in the same lane, where it runs serially.
        parent = list(range(len(members)))

        def find(node: int) -> int:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        reasons: list[str] = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                why = _conflict(members[i], members[j], depends)
                if why is None:
                    continue
                reasons.append(f"{members[i]['prompt']} + {members[j]['prompt']}: {why}")
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
        groups: dict[int, list[dict]] = {}
        for position, member in enumerate(members):
            groups.setdefault(find(position), []).append(member)
        lanes = [groups[root] for root in sorted(groups, key=lambda root: index[
            (groups[root][0]["app"], groups[root][0]["stem"])])]
        phases.append(
            {
                "level": level,
                "lanes": [
                    {"steps": lane, "invocations": _group_invocations(lane)} for lane in lanes
                ],
                "serial_reasons": sorted(set(reasons)),
            }
        )
    return phases


def _group_invocations(steps: list[dict]) -> list[dict]:
    """Adjacent steps for the same repository collapse into one invocation."""
    grouped: list[dict] = []
    for step in steps:
        if grouped and grouped[-1]["alias"] == step["alias"]:
            grouped[-1]["prompts"].append(step["prompt"])
            continue
        grouped.append({"alias": step["alias"], "prompts": [step["prompt"]]})
    return grouped


# ---------------------------------------------------------------------------
# Rendering — the copyable part
# ---------------------------------------------------------------------------
def _invocation(alias: str, prompts: list[str], indent: str = "") -> list[str]:
    """One `run-sequence.sh --queue <alias> <prompt>...` invocation.

    A continuation backslash is the LAST character of its line — a trailing
    space after one turns the next line into a fresh command and is invisible in
    every editor. `run-sequence.sh` is spelled without `./` so the block is
    pasteable from any directory that has it on PATH.

    The alias is printed EXACTLY as `workspace.json` spells it. Alias lookup is
    case-insensitive, so a lowercased one would also work, but a plan that
    renamed the operator's aliases in its own output is a plan they have to
    translate before they can trust it.
    """
    if len(prompts) == 1:
        return [f"{indent}run-sequence.sh --queue {alias} {prompts[0]}"]
    lines = [f"{indent}run-sequence.sh --queue {alias} \\"]
    for position, prompt in enumerate(prompts):
        tail = "" if position == len(prompts) - 1 else " \\"
        lines.append(f"{indent}  {prompt}{tail}")
    return lines


def render_serial(steps: list[dict]) -> str:
    if not steps:
        return ""
    lines: list[str] = []
    groups = _group_invocations(steps)
    for position, group in enumerate(groups):
        block = _invocation(group["alias"], group["prompts"])
        if position < len(groups) - 1:
            block[-1] = block[-1] + " && \\"
        lines.extend(block)
    return "\n".join(lines) + "\n"


def render_parallel(phases: list[dict]) -> str:
    """A single orchestration block, or "" when nothing is proven parallel.

    The shape is deliberate: every lane's PID is kept, EVERY lane is waited for,
    a failure in ANY lane (not merely the last one) sets the flag, and a
    dependent phase is only entered after the barrier passes. A `wait` on the
    last PID alone would report success while an earlier lane had failed.

    IT IS COMMENTED, BECAUSE THE MACHINERY IS THE UNFAMILIAR PART. `x=$!` and
    `wait "$x" || flag=1` are ordinary shell, but a reader meeting them between
    two long prompt filenames reads them as noise, or as errors the tool printed
    at them — which is what happened. The comments travel WITH the paste, where
    the report's prose above the block does not, so they belong in the block.
    They are the reason to print a plan rather than execute one: an operator can
    check it first, and can only check what they can read.

    The shell bookkeeping is explained ONCE, in the header, and each phase and
    barrier then gets one line. Repeating the explanation at every phase was the
    first attempt and it buried the commands it was there to introduce.
    """
    if not any(len(phase["lanes"]) > 1 for phase in phases):
        return ""
    total = len(phases)
    lines: list[str] = [
        "# ==========================================================================",
        "# A DEPENDENCY-ORDERED PLAN, IN PHASES",
        "# ==========================================================================",
        "# Everything inside one phase runs at the same time; each phase waits for the",
        "# whole of the phase before it. Paste this into a terminal as it is, or save",
        "# it as a script and run that.",
        "#",
        "# The lines that are not run-sequence.sh invocations are shell bookkeeping —",
        "# not errors, and not output:",
        "#",
        "#   <command> &                start a lane in the BACKGROUND, without",
        "#                              waiting for it to finish",
        "#   phase2_lane1=$!            remember the process id of the lane just",
        "#                              started, so it can be waited for by name",
        "#   wait \"$phase2_lane1\" ...   block until that exact lane has finished",
        "#",
        "# Each barrier waits for EVERY lane in its phase and fails if ANY of them",
        "# failed — waiting on the last lane alone would report success over a lane",
        "# that had already died.",
    ]
    for position, phase in enumerate(phases):
        number = position + 1
        lanes = phase["lanes"]
        lines.append("")
        if len(lanes) == 1:
            lines.append(f"# PHASE {number} of {total} — ONE LANE, run in this order.")
            lines.append("#   Nothing in it was proven independent of anything else here.")
            for group_index, group in enumerate(lanes[0]["invocations"]):
                block = _invocation(group["alias"], group["prompts"])
                if group_index < len(lanes[0]["invocations"]) - 1:
                    block[-1] = block[-1] + " && \\"
                lines.extend(block)
            continue
        lines.append(
            f"# PHASE {number} of {total} — {len(lanes)} LANES, running AT THE SAME TIME."
        )
        lines.append(
            "#   Each was proven independent of the others: no dependency either way,"
        )
        lines.append(
            "#   disjoint declared mutation targets, different prompt/handoff folders."
        )
        names: list[str] = []
        for lane_index, lane in enumerate(lanes):
            name = f"phase{number}_lane{lane_index + 1}"
            names.append(name)
            invocations = lane["invocations"]
            lines.append("")
            lines.append(f"# lane {lane_index + 1} of {len(lanes)}")
            if len(invocations) == 1:
                block = _invocation(invocations[0]["alias"], invocations[0]["prompts"])
                block[-1] = block[-1] + " &"
                lines.extend(block)
            else:
                lines.append("(")
                for group_index, group in enumerate(invocations):
                    block = _invocation(group["alias"], group["prompts"], indent="  ")
                    if group_index < len(invocations) - 1:
                        block[-1] = block[-1] + " && \\"
                    lines.extend(block)
                lines.append(") &")
            lines.append(f"{name}=$!")
        lines.append("")
        lines.append(
            f"# BARRIER — wait for all {len(lanes)} lanes above; any failure stops the plan here."
        )
        lines.append("phase_failed=0")
        for name in names:
            lines.append(f'wait "${name}" || phase_failed=1')
        lines.append("(( phase_failed == 0 )) || exit 1")
    return "\n".join(lines) + "\n"


def render_human(plan: dict) -> str:
    out: list[str] = []
    out.append("run-sequence.sh --extract-sequence — READ-ONLY PLAN")
    out.append("")
    out.append("Nothing was started, moved, written or committed to produce this.")
    out.append("")

    counts: dict[str, int] = {state: 0 for state in STATES}
    for entry in plan["census"]:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    out.append("STATE SUMMARY")
    out.append(f"  repositories inspected:  {len(plan['repositories'])}")
    out.append(f"  prompts discovered:      {len(plan['census'])}")
    for state in STATES:
        out.append(f"  {state + ':':24s} {counts.get(state, 0)}")
    out.append(f"  scheduled in this plan:  {len(plan['steps'])}")
    out.append("")

    if plan.get("malformed"):
        out.append("UNREADABLE FRONTMATTER — these prompts have UNKNOWN dependencies")
        out.append("Fix them before trusting any order below; nothing here was assumed")
        out.append("to declare nothing. `scripts/prompt_frontmatter.py lint <file>`.")
        for entry in plan["malformed"]:
            out.append(f"  {entry['app']}/{entry['prompt']}")
            out.append(f"      {entry['error']}")
        out.append("")

    if plan["cycles"]:
        out.append("DEPENDENCY CYCLES — nothing in one can ever be scheduled")
        for cycle in plan["cycles"]:
            out.append("  " + " -> ".join(cycle))
        out.append("")

    if plan["repository_errors"]:
        out.append("REPOSITORIES EXCLUDED")
        for entry in plan["repository_errors"]:
            out.append(f"  {entry['alias']}: {entry['reason']}")
        out.append("")

    if plan["excluded"]:
        out.append("EXCLUDED AND DEFERRED PROMPTS — with the reason, never silently")
        for entry in plan["excluded"]:
            out.append(f"  [{entry['state']}] {entry['app']}/{entry['prompt']}")
            out.append(f"      {entry['reason']}")
        out.append("")

    if not plan["steps"]:
        # Two different facts wear the same headline otherwise, and they call for
        # opposite reactions: an empty queue is finished work, while a queue full
        # of prompts none of which can start is something to go and unblock.
        outstanding = [entry for entry in plan["census"] if entry["location"] == "queue"]
        if outstanding:
            out.append("NOTHING TO RUN — no queued prompt is currently schedulable.")
        else:
            out.append(
                "NOTHING TO RUN — every queue inspected is empty. Nothing is "
                "outstanding:"
            )
            out.append(
                f"  all {len(plan['census'])} prompts found are in "
                f"{DONE_DIRNAME}/ or {BLOCKED_DIRNAME}/."
            )
        return "\n".join(out) + "\n"

    out.append("RECOMMENDED SERIAL COMMAND — safe, and the one to paste if unsure")
    out.append("")
    out.extend(plan["serial_command"].rstrip("\n").splitlines())
    out.append("")

    parallel = plan["parallel_command"]
    if parallel:
        out.append("PARALLEL LANES — the same plan with every provably independent pair")
        out.append("running at once. Anything unproven stays serial. The block explains")
        out.append("its own shell, because the explanation has to survive being pasted.")
        out.append("")
        out.extend(parallel.rstrip("\n").splitlines())
        out.append("")
    else:
        out.append("NO PARALLEL LANES — nothing in this plan is provably independent.")
        reasons = sorted({reason for phase in plan["phases"] for reason in phase["serial_reasons"]})
        for reason in reasons[:12]:
            out.append(f"  {reason}")
        out.append("")
    return "\n".join(out) + "\n"


def render_shell(plan: dict) -> str:
    """Executable shell and nothing else — no banner, no prose, no prefixes."""
    return plan["serial_command"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only run-sequence planner")
    parser.add_argument("--config", type=Path, help="an explicit workspace.json")
    parser.add_argument("--queue", action="append", default=[], metavar="ALIAS")
    parser.add_argument("--format", choices=("human", "shell", "json"), default="human")
    args = parser.parse_args()

    try:
        plan = build_plan(workspace_config.load(args.config), args.queue)
    except workspace_config.WorkspaceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(plan, indent=2, sort_keys=True, default=str))
    elif args.format == "shell":
        sys.stdout.write(render_shell(plan))
    else:
        sys.stdout.write(render_human(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
