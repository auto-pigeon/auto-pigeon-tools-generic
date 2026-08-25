#!/usr/bin/env python3
"""Resolve which prompt (if any) is next to run for one repository.

COMPLETION IS A FILESYSTEM FACT, NOT A PARSED FIELD
---------------------------------------------------
A prompt's state is *where its file is*, not what some other file says
about it:

    <data_root>/LLM/prompts/<app>/            outstanding — this IS the queue
    <data_root>/LLM/prompts/<app>/done/       finished, never picked again
    <data_root>/LLM/prompts/<app>/blocked/    attempted and genuinely stuck

"What's next" is therefore: the oldest file *directly* in
`LLM/prompts/<app>/`, by filename sort. Full stop. No handoff is read to
answer that question, which is what makes the rule stable enough to hand
to an agent in prose ("that folder is the queue") without it being able
to misjudge the order — the failure that made a queue's prompts 22-24
unreachable once 25 existed (see the toolkit's WORKFLOW.md).

Completing a prompt means moving its file into `done/` as the task's own
last step, after its handoff is written. Handoffs are still mandatory —
they carry the audit trail — they just no longer *gate* anything.

Handoff status is still read for exactly two secondary purposes, neither
of which is selection:

  * `resume` — telling a resumed prompt (`in_progress`/`partial`/`failed`
    handoff) apart from a fresh one, so the agent is told to continue
    rather than restart.
  * the completed-but-not-moved guard — a prompt still in the queue whose
    handoff says `complete` for this exact file is an inconsistency, not
    work to redo. It is reported `blocked` with the fix, never silently
    re-run.

Prerequisites (machine-readable `requires:` frontmatter if present,
otherwise parsed from a `## Prerequisite` prose section, for prompts
written before that convention) are checked recursively against `done/`
presence, and the *root* unmet one is reported, not just the immediate
one.

Usage:
    resolve_next_prompt.py --repo ALIAS --data-root PATH [--skip STEM]... [--json]
    resolve_next_prompt.py --data-root PATH --dependents-of [ALIAS/]STEM... [--json]

`--skip` defers a prompt FOR THIS RUN ONLY: it is hidden from selection, its
file is not moved and no handoff is written for it. That is how
run-sequence.sh keeps draining a queue after one prompt blocked.

`--dependents-of` answers the other half of the same question — which queued
prompts, in any app, transitively require the ones named — so the runner can
defer them without asking an LLM to remember them.

Exit code is always 0; the result (run / blocked / idle / error / dependents)
is in the output. Callers branch on the `action` field, not on exit status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prompt_frontmatter as fm  # noqa: E402
from prompt_frontmatter import FrontmatterError  # noqa: E402

PROMPT_NAME_RE = re.compile(
    r"(?P<stem>\d{8}(?:T\d{6}Z)?_\d{2}[_-][\w-]+|\d{2}-[\w-]+)"
)
#: Kept as the module's public spelling of "where the block is" for the readers
#: that only need to know whether one exists. DECODING it is
#: `prompt_frontmatter`'s job and nothing here may do it by hand again.
FRONTMATTER_RE = fm.FRONTMATTER_RE
PREREQ_SECTION_RE = re.compile(
    r"^##\s*Prerequisite\b.*?(?=^##\s|\Z)", re.DOTALL | re.MULTILINE
)

DONE_DIRNAME = "done"
BLOCKED_DIRNAME = "blocked"


@dataclass(frozen=True)
class Requirement:
    """One declared prerequisite.

    `app` is the prompt folder it lives in — which is a repository alias — and
    None means "this repo". Both forms appear in real prompts:

        requires: [20260801_16_Some-Prompt]           # same repo

        requires:                                     # cross-repo
          - repo: api
            prompt: 20260803_20_Add-Session-Endpoint.md
    """

    stem: str
    app: str | None = None

    @property
    def label(self) -> str:
        return f"{self.app}/{self.stem}" if self.app else self.stem


@dataclass
class HandoffInfo:
    path: Path
    prompt_path: str | None
    status: str | None
    prompt_sha256: str | None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Top-level scalars only — the shape the handoff readers here want.

    Tolerant by construction: this is only ever asked about a HANDOFF, which is
    read for two secondary purposes (resume, and the completed-but-not-moved
    guard) and never carries a dependency edge. A handoff nobody can parse is a
    `lint` finding, not a reason to refuse to pick the next prompt.
    """
    try:
        return fm.parse(text, inline_comments=False).scalars()
    except FrontmatterError:
        return {}


def _load_handoffs(handoff_dir: Path) -> dict[str, HandoffInfo]:
    """Map prompt basename (no extension) -> its handoff info, if any."""
    by_prompt: dict[str, HandoffInfo] = {}
    if not handoff_dir.is_dir():
        return by_prompt
    for path in sorted(handoff_dir.glob("*.md")):
        fields = _parse_frontmatter(_read_text(path))
        prompt_path = fields.get("prompt_path")
        key = Path(prompt_path).stem if prompt_path else path.stem
        # If more than one handoff references the same prompt (shouldn't
        # normally happen), the most recently modified one wins — it's the
        # most likely to be the corrected/final account.
        existing = by_prompt.get(key)
        if existing is None or path.stat().st_mtime >= existing.path.stat().st_mtime:
            by_prompt[key] = HandoffInfo(
                path=path,
                prompt_path=prompt_path,
                status=fields.get("status"),
                prompt_sha256=fields.get("prompt_sha256"),
            )
    return by_prompt


def _clean(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    return value[:-3] if value.endswith(".md") else value


#: Spellings of "this prompt declares no prerequisites", as a SCALAR value.
#: An empty block list means the same thing and reaches here as `[]`.
_NO_REQUIREMENTS = ("", "[]", "none", "null", "n/a", "-")

#: Inside a mapping list item, which key names the other repository and which
#: names the prompt. `- repo: api` / `  prompt: X.md`.
_REQ_APP_KEYS = ("repo", "repository", "app")
_REQ_STEM_KEYS = ("prompt", "stem", "path", "task", "task_id")


def _requirement_from_mapping(item: dict, path: Path) -> Requirement:
    """One cross-repository prerequisite out of a mapping list item.

    Taking the text after the dash was the bug that produced prerequisites
    literally named "repo: api", which could never be satisfied and
    reported a nonsense root cause.
    """
    app = next((str(item[key]) for key in _REQ_APP_KEYS if item.get(key)), None)
    stem = next((str(item[key]) for key in _REQ_STEM_KEYS if item.get(key)), None)
    if not stem:
        raise FrontmatterError(
            "unnamed_requirement",
            "a `requires:` mapping item names a repository but no prompt "
            f"(keys: {', '.join(sorted(item)) or 'none'}); an edge that names "
            "nothing cannot be checked and must not be read as no edge",
            path=path,
        )
    return Requirement(stem=_clean(stem), app=_clean(app) if app else None)


def _requires_from_frontmatter(path: Path, text: str) -> list[Requirement] | None:
    """The declared `requires:`, or None when the prompt declares none at all.

    ONE call into the canonical decoder. Every accepted spelling — `-` bullets,
    `*` bullets, a flow list, a bare scalar, an indented key after a
    de-indented list — is that decoder's business, not this function's, which
    is the entire point of the one decoder: three readers cannot disagree
    about a file none of them parses.
    """
    block = fm.parse(text, path=path, inline_comments=True)
    if not block.has("requires"):
        return None
    scalar = block.field_scalar("requires")
    if scalar is not None and scalar.strip().lower() in _NO_REQUIREMENTS:
        return []
    items = block.field_list("requires") or []
    requirements: list[Requirement] = []
    for item in items:
        if isinstance(item, dict):
            requirements.append(_requirement_from_mapping(item, path))
        elif isinstance(item, str) and item.strip():
            requirements.append(Requirement(stem=_clean(item)))
    return requirements


def _requires_from_prose(text: str) -> list[Requirement]:
    section_match = PREREQ_SECTION_RE.search(text)
    if not section_match:
        return []
    return [
        Requirement(stem=stem)
        for stem in sorted(set(m.group("stem") for m in PROMPT_NAME_RE.finditer(section_match.group(0))))
    ]


def extract_requires(prompt_path: Path) -> list[Requirement]:
    """Every declared prerequisite of one prompt.

    Raises `FrontmatterError` when the block cannot be read. It used to return
    `[]` — the same value as "declares nothing" — and a real prompt was
    scheduled with an invisible prerequisite because of it. A caller that wants
    to keep going past a malformed prompt has to say so, in writing, at its own
    call site; none of them may spell it as an empty list.
    """
    text = _read_text(prompt_path)
    explicit = _requires_from_frontmatter(prompt_path, text)
    if explicit is not None:
        return explicit
    # No `requires:` key at all: a prompt written before the convention. Its
    # prerequisites are whatever its `## Prerequisite` prose names.
    return _requires_from_prose(text)


def queue_prompts(prompt_dir: Path) -> list[Path]:
    """Outstanding prompts: *.md directly in the folder, oldest first.

    Non-recursive on purpose — `done/` and `blocked/` are subdirectories,
    so they are excluded by construction rather than by a filter that
    could be forgotten.
    """
    return sorted(p for p in prompt_dir.glob("*.md") if p.is_file())


def _stems(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {p.stem for p in directory.glob("*.md") if p.is_file()}


# ---------------------------------------------------------------------------
# The deterministic dependency closure — the FLOOR under queue-aware blocking
# ---------------------------------------------------------------------------
# When a prompt blocks, which of the prompts still queued cannot honestly be
# attempted? An agent's own assessment is asked for and recorded (see the
# `block:` metadata agent_task.py writes), but it is not trusted on its own: an
# LLM that forgets to list a dependant would let the runner start work whose
# prerequisite is missing. This computes the answer from the SAME `requires:`
# parser selection already uses, across every app's queue folder, so a
# cross-repository prerequisite is seen exactly as the resolver sees it.
#
# It walks to a fixpoint, so C requires B requires A is deferred when A blocks
# even though C never mentions A.


def _app_queues(data_root: Path) -> list[Path]:
    prompts_root = data_root / "LLM" / "prompts"
    if not prompts_root.is_dir():
        return []
    return sorted(p for p in prompts_root.iterdir() if p.is_dir())


def queued_index(data_root: Path) -> list[dict]:
    """Every prompt STILL IN A QUEUE FOLDER, with its requirements resolved.

    Only queued prompts: a prompt in done/ cannot be deferred, and one in
    blocked/ is already out of the run.
    """
    entries: list[dict] = []
    for app_dir in _app_queues(data_root):
        app = app_dir.name
        for path in queue_prompts(app_dir):
            malformed: str | None = None
            try:
                requires = {
                    (requirement.app or app, requirement.stem)
                    for requirement in extract_requires(path)
                }
            except FrontmatterError as error:
                # A prompt whose edges cannot be read has UNKNOWN edges, and the
                # safe reading of unknown, in a function whose job is to decide
                # what a block takes down with it, is "it might depend on this".
                # `dependents_of` therefore defers it rather than clearing it.
                requires = set()
                malformed = str(error)
            entries.append(
                {
                    "app": app,
                    "prompt": path.name,
                    "stem": path.stem,
                    "requires": sorted(requires),
                    "malformed": malformed,
                }
            )
    return entries


def parse_seed(label: str) -> tuple[str | None, str]:
    """`app/stem`, `app:stem` or a bare `stem` (which matches in any app)."""
    for separator in ("/", ":"):
        if separator in label:
            app, _, stem = label.partition(separator)
            return (app.strip() or None), _clean(stem.strip())
    return None, _clean(label.strip())


def dependents_of(data_root: Path, labels: list[str]) -> dict:
    seeds = [parse_seed(label) for label in labels]
    entries = queued_index(data_root)

    def matches(requirement: tuple[str, str], seed: tuple[str | None, str]) -> bool:
        seed_app, seed_stem = seed
        req_app, req_stem = requirement
        if req_stem != seed_stem:
            return False
        return seed_app is None or req_app == seed_app

    closure: dict[tuple[str, str], dict] = {}
    frontier: list[tuple[str | None, str]] = list(seeds)
    while frontier:
        seed = frontier.pop(0)
        for entry in entries:
            key = (entry["app"], entry["stem"])
            if key in closure:
                continue
            hit = next(
                (requirement for requirement in entry["requires"] if matches(requirement, seed)),
                None,
            )
            if hit is None and not entry["malformed"]:
                continue
            closure[key] = {
                "app": entry["app"],
                "prompt": entry["prompt"],
                "stem": entry["stem"],
                "via": f"{hit[0]}/{hit[1]}" if hit else "unreadable frontmatter",
                "malformed": entry["malformed"],
            }
            frontier.append((entry["app"], entry["stem"]))

    return {
        "action": "dependents",
        "seeds": [f"{app}/{stem}" if app else stem for app, stem in seeds],
        "dependents": [closure[key] for key in sorted(closure)],
        "queued_total": len(entries),
    }


def resolve(
    alias: str,
    data_root: Path,
    skip: set[str] | None = None,
    assume_done: set[tuple[str, str]] | None = None,
) -> dict:
    """Which prompt (if any) is next for the repository configured as `alias`.

    The alias comes from `workspace.json` — it is both the repository's name and
    the name of its queue and handoff folders. The source toolkit read those
    folder names out of a configuration file committed inside each checkout;
    that file is gone, and with it the possibility of a repository disagreeing
    with the workspace about which queue is its own.
    """
    alias_dir = alias

    prompt_dir = data_root / "LLM" / "prompts" / alias_dir
    handoff_dir = data_root / "LLM" / "handoffs" / alias_dir
    done_dir = prompt_dir / DONE_DIRNAME
    blocked_dir = prompt_dir / BLOCKED_DIRNAME
    if not prompt_dir.is_dir():
        return {"action": "idle", "reason": f"no prompt directory at {prompt_dir}"}

    # PLANNING OVERLAY — `assume_done` is how the read-only planner drives THIS
    # function instead of reimplementing it. It names (app, stem) pairs to treat
    # as if their files already sat in done/, which is exactly the state the
    # queue would be in after the earlier steps of a plan had run. Nothing on
    # disk is touched and the default (None) leaves every answer byte-identical,
    # so the executor's path is unchanged. It is what makes "the planner and the
    # executor resolve the same next prompt" a property of one implementation
    # rather than of two that agree today.
    assume_done = assume_done or set()
    assumed_here = {stem for app, stem in assume_done if app == alias_dir}

    prompts = [p for p in queue_prompts(prompt_dir) if p.stem not in assumed_here]
    done_stems = _stems(done_dir) | assumed_here
    blocked_stems = _stems(blocked_dir)

    # DEFERRED-FOR-THIS-RUN, and nothing more. `--skip` hides a prompt from
    # SELECTION only: the file stays exactly where it is, no handoff is written
    # for it, and the next run with no `--skip` picks it up again in its normal
    # place. It is how run-sequence.sh keeps working through a queue after one
    # prompt blocked, without the runner having to reimplement this parser.
    # Prerequisite checks below deliberately still see a skipped prompt as
    # OUTSTANDING, because that is what it is.
    skip = skip or set()
    deferred = [p for p in prompts if p.stem in skip]
    selectable = [p for p in prompts if p.stem not in skip]

    if not selectable:
        if deferred:
            return {
                "action": "idle",
                "reason": (
                    f"every remaining prompt in {prompt_dir} is deferred for this run "
                    f"({len(deferred)}): {', '.join(sorted(p.stem for p in deferred))}"
                ),
                "deferred_count": len(deferred),
                "done_count": len(done_stems),
                "blocked_count": len(blocked_stems),
            }
        if done_stems or blocked_stems:
            reason = (
                f"queue is empty: every prompt in {prompt_dir} has been moved to "
                f"{DONE_DIRNAME}/ ({len(done_stems)}) or {BLOCKED_DIRNAME}/ "
                f"({len(blocked_stems)})"
            )
        else:
            reason = f"no prompts in {prompt_dir}"
        return {
            "action": "idle",
            "reason": reason,
            "done_count": len(done_stems),
            "blocked_count": len(blocked_stems),
        }

    candidate = selectable[0]

    # --- completed-but-not-moved guard ---
    # Under this model a finished prompt leaves the queue. One that is still
    # here but already has a `complete` handoff pinned to this exact file is
    # an inconsistency (an interrupted task, or an agent that wrote the
    # handoff and never made the move). Redoing finished work silently is the
    # worst possible response, so say so loudly and let a human or the next
    # agent make the one-line correction.
    #
    # THIS RUNS BEFORE THE PREREQUISITE CHASE, and the order is the point. It is
    # a statement about THIS FILE's own state, and nothing another queue does can
    # change it — so it must not be maskable by one. It used to sit below the
    # chase, and a real prompt is what that cost: it was finished and never
    # moved, and because it ALSO declared a prerequisite still outstanding in
    # another repository, the report said "requires <other>/<prompt>, which is
    # not in done/" — sending an operator to wait for work that would change
    # nothing, while the one-line fix (move the file) went unmentioned. Worse,
    # the diagnosis was not stable: a whole-workspace plan that happened to
    # schedule that prerequisite first reported the same file as
    # completed-but-not-moved, so the answer depended on which queues were in
    # scope. A prompt's own state is not a matter of scope.
    handoffs = _load_handoffs(handoff_dir)
    existing = handoffs.get(candidate.stem)

    if existing is not None and existing.status == "complete":
        current_sha = hashlib.sha256(_read_text(candidate).encode("utf-8")).hexdigest()
        if not existing.prompt_sha256 or existing.prompt_sha256.strip() == current_sha:
            return {
                "action": "blocked",
                "reason": (
                    f"{candidate.name} is still in the queue but {existing.path.name} "
                    f"already records it complete for this exact file. It was finished "
                    f"and never moved. Move it into {done_dir} (or delete the stale "
                    "handoff if it is wrong) — it must not be silently re-run."
                ),
                "candidate": candidate.name,
                "reason_code": "completed_but_not_moved",
                "handoff": str(existing.path),
            }
        # SHA differs: the prompt was rewritten after being completed, so the
        # handoff describes a different text. That is real new work.

    resume = existing is not None and existing.status in ("in_progress", "partial", "failed")

    # --- prerequisite chain: walk to the root cause, not just the first gap ---
    # A prerequisite is satisfied iff its file sits in done/. Anything else —
    # still queued, sitting in blocked/, or not present at all — is unmet.
    def locate(stem: str) -> Path | None:
        for directory in (prompt_dir, done_dir, blocked_dir):
            matches = sorted(directory.glob(f"{stem}*.md")) if directory.is_dir() else []
            if matches:
                return matches[0]
        return None

    def unmet_because(requirement: Requirement) -> str | None:
        if requirement.app and requirement.app != alias_dir:
            # Cross-repo prerequisite: the same question, asked of another
            # app's queue folder. Presence in that app's done/ is the answer;
            # its own prerequisite chain is that repo's business, not ours.
            other = data_root / "LLM" / "prompts" / requirement.app
            if not other.is_dir():
                return f"{requirement.app} has no prompt folder at {other}"
            pattern = f"{requirement.stem}*.md"
            if (requirement.app, requirement.stem) in assume_done:
                return None
            if requirement.stem in _stems(other / DONE_DIRNAME):
                return None
            if any(other.glob(pattern)):
                return f"it is still outstanding in {requirement.app}'s queue"
            if requirement.stem in _stems(other / BLOCKED_DIRNAME):
                return f"it is parked in {requirement.app}'s {BLOCKED_DIRNAME}/"
            return f"no prompt with that name exists in {requirement.app}'s queue"
        stem = requirement.stem
        if stem in done_stems:
            return None
        if stem in blocked_stems:
            return f"it is parked in {BLOCKED_DIRNAME}/"
        if any(p.stem == stem for p in prompts):
            return "it is still outstanding in the queue"
        if locate(stem) is not None:
            return "it exists but is not in done/"
        return "no prompt with that name exists in this repo's queue"

    seen: set[str] = set()
    frontier = [Requirement(stem=candidate.stem)]
    unmet: dict[str, str] = {}
    while frontier:
        requirement = frontier.pop(0)
        if requirement.label in seen:
            continue
        seen.add(requirement.label)
        if requirement.app and requirement.app != alias_dir:
            continue  # not our chain to walk
        req_path = locate(requirement.stem)
        if req_path is None:
            continue
        # MALFORMED FRONTMATTER IS A REFUSAL, NOT AN EMPTY LIST. A prompt whose
        # dependency block cannot be read is a prompt whose prerequisites are
        # unknown, and scheduling on an unknown prerequisite is the failure this
        # whole task exists to remove. Naming the file and the line is what makes
        # it a one-line fix instead of an investigation.
        try:
            children = extract_requires(req_path)
        except FrontmatterError as error:
            return {
                "action": "blocked",
                "reason": (
                    f"{candidate.name} cannot be scheduled: {error}. Its dependency "
                    "declaration could not be decoded, so whether its prerequisites "
                    "are met is unknown. Fix the frontmatter (see "
                    "`scripts/prompt_frontmatter.py lint`) — it must not be run on "
                    "the assumption that it declares nothing."
                ),
                "candidate": candidate.name,
                "reason_code": "malformed_frontmatter",
                "malformed_prompt": str(req_path),
                "frontmatter_error": str(error),
            }
        for child in children:
            why = unmet_because(child)
            if why is not None:
                unmet[child.label] = why
                frontier.append(child)

    if unmet:
        # The root cause is whichever unmet prerequisite is itself furthest
        # back in the queue (oldest) — that's the one actually blocking
        # everything downstream of it, not necessarily the first one found.
        root_cause = sorted(unmet)[0]
        return {
            "action": "blocked",
            "reason": (
                f"{candidate.name} requires {root_cause}, which is not in "
                f"{DONE_DIRNAME}/ — {unmet[root_cause]} (checked recursively "
                "through the prerequisite chain, not just the immediate "
                "prerequisite)."
            ),
            "candidate": candidate.name,
            "root_cause": root_cause,
            "unmet_chain": sorted(unmet),
        }

    return {
        "action": "run",
        "prompt_path": str(candidate),
        "prompt_text": _read_text(candidate),
        "resume": resume,
        "resume_status": existing.status if resume else None,
        "queue_length": len(prompts),
        "done_count": len(done_stems),
        "blocked_count": len(blocked_stems),
        "done_dir": str(done_dir),
        "blocked_dir": str(blocked_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="the repository alias from workspace.json")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="STEM",
        help="do not select this prompt (deferred for THIS run; the file is not touched)",
    )
    parser.add_argument(
        "--dependents-of",
        action="append",
        default=[],
        metavar="[APP/]STEM",
        help="instead of selecting: print the transitive closure of queued prompts that "
        "depend on these, across every app queue",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output only")
    args = parser.parse_args()

    if args.dependents_of:
        result = dependents_of(args.data_root.resolve(), args.dependents_of)
        if args.json:
            print(json.dumps(result))
        else:
            for entry in result["dependents"]:
                print(f"{entry['app']}/{entry['prompt']}\trequires {entry['via']}")
        return 0

    if not args.repo:
        parser.error("--repo is required unless --dependents-of is given")

    result = resolve(
        args.repo,
        args.data_root.resolve(),
        skip={_clean(stem) for stem in args.skip},
    )

    if args.json:
        print(json.dumps(result))
    else:
        action = result.get("action")
        if action == "run":
            print(result["prompt_text"])
        else:
            print(f"{action}: {result.get('reason', '')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
