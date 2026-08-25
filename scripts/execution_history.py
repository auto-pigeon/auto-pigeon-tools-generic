#!/usr/bin/env python3
"""Execution history behind `run-sequence.sh --history`. READ-ONLY.

It answers one question — how long has each prompt actually taken, and how much
time is in the whole record — from the handoffs, with the runner's own attempt
ledgers as a fallback for prompts whose handoff predates timing.

THE TOTAL IS A SUM, NOT A SPAN
------------------------------
`Total execution time` is the sum of every prompt's `execution_seconds`. It is
NOT the wall clock between the earliest start and the latest finish, and the
difference is the whole point: two prompts run in parallel lanes each consumed
their full duration, and a report that collapsed them into elapsed calendar time
would say a night's work cost half what it did. A twelve-hour day of two
concurrent lanes reads as roughly twenty-four hours here, correctly.

By the same token this is not CPU time, not token usage and not billed time —
see `execution_time.py` for the definition it is measuring.

WHAT IS COUNTED ONCE
--------------------
One logical entry is (canonical repository, prompt identity). A partial handoff
later updated to complete is ONE entry, because it is one file. The final
canonical handoff always wins over runner state; the ledger is consulted only
when the handoff has no timing at all, so an attempt can never be added from
both. Where identity is genuinely ambiguous — two handoff files claiming the same
prompt — the ambiguity is REPORTED and the entry is left out of the total, which
is the only answer that cannot be quietly wrong.

A LEGACY HANDOFF IS `unknown`, NEVER ZERO. Counting an unmeasured prompt as zero
seconds makes a total that looks precise and is not. They are counted, listed and
excluded from the sum, and the report says how many.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import execution_time  # noqa: E402
import prompt_frontmatter  # noqa: E402
import workspace_config  # noqa: E402

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
COMPLETED_STATUSES = {"complete"}
PARTIAL_STATUSES = {"partial", "in_progress", "failed"}


def _frontmatter(path: Path) -> dict[str, str]:
    """Top-level scalars, via the one canonical decoder.

    Tolerant: a handoff whose header cannot be decoded is a `lint` finding and
    an `unknown` row in this report, never a crashed report. `--history` is
    read-only and its whole job is to say what IS recorded.
    """
    try:
        return prompt_frontmatter.parse_handoff_tolerant(path).scalars()
    except OSError:
        return {}


def _identity(declared: str | None) -> str | None:
    """The prompt stem a handoff pins, or None when it pins no prompt at all.

    `agent_task.py` writes a queue-relative `LLM/prompts/<app>/<file>.md`. A
    manual-work handoff has no prompt, and what sits in the field then is `null`
    or a sentence — neither of which is an identity, and both of which must not
    be mistaken for one.
    """
    if not declared:
        return None
    value = declared.strip().strip("\"'")
    if not value or value.lower() in ("null", "none", "~", "-"):
        return None
    if not value.endswith(".md") or " " in value:
        return None
    return Path(value).stem


def _seconds(raw: str | None, warnings: list[str], where: str) -> int | None:
    """A strict integer, or None with a warning. Never a silent zero."""
    if raw is None or raw == "" or raw == "null":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        try:
            floated = float(raw)
        except (TypeError, ValueError):
            warnings.append(f"{where}: execution_seconds is not a number ({raw!r}) — excluded")
            return None
        warnings.append(
            f"{where}: execution_seconds is not an integer ({raw!r}) — excluded"
        )
        del floated
        return None
    if value < 0:
        warnings.append(f"{where}: execution_seconds is negative ({value}) — excluded")
        return None
    return value


def _ledger_totals(data_root: Path) -> dict[str, dict]:
    """Every runner attempt ledger on disk, keyed by prompt filename.

    Attempts from DIFFERENT runs of the same prompt are separate attempts and are
    summed; attempts from the same run are already deduplicated inside the
    ledger, which stores one record per attempt number.
    """
    root = data_root / ".run-sequence"
    found: dict[str, dict] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/timing/*/*.attempts.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for attempt in payload.get("attempts") or []:
            prompt = attempt.get("prompt")
            if not prompt:
                continue
            bucket = found.setdefault(
                prompt, {"seconds": 0, "attempts": 0, "starts": [], "finishes": [], "runs": set()}
            )
            bucket["runs"].add(path.parts[-4])
            bucket["attempts"] += 1
            bucket["seconds"] += attempt.get("elapsed_seconds") or 0
            if attempt.get("started_at"):
                bucket["starts"].append(attempt["started_at"])
            if attempt.get("finished_at"):
                bucket["finishes"].append(attempt["finished_at"])
    return found


def collect(workspace: workspace_config.Workspace, wanted: list[str] | None = None) -> dict:
    data_root = workspace.data_root
    handoff_root = data_root / "LLM" / "handoffs"
    warnings: list[str] = []

    directories: list[str] = []
    alias_of: dict[str, str] = {}
    for repo in workspace.repositories:
        # The handoff folder is the alias, and a repository whose checkout is
        # currently missing still has history worth reporting — history is read
        # from handoffs, never from the repositories themselves.
        if (handoff_root / repo.alias).is_dir():
            directories.append(repo.alias)
            alias_of[repo.alias] = repo.alias
    # A handoff folder the configuration never named is still real history — a
    # repository retired from workspace.json did not un-run its prompts. With no
    # repository filter, every canonical handoff directory is inspected.
    if handoff_root.is_dir():
        for path in sorted(handoff_root.iterdir()):
            if path.is_dir() and path.name not in directories:
                directories.append(path.name)
                alias_of.setdefault(path.name, path.name)

    if wanted:
        # An alias or a directory. Silently matching nothing would report an empty
        # history as though no prompt had ever run in that repository.
        wanted_lower = {value.lower() for value in wanted}
        selected = [
            directory
            for directory in directories
            if alias_of[directory].lower() in wanted_lower or directory.lower() in wanted_lower
        ]
        matched = {value.lower() for directory in selected
                   for value in (alias_of[directory], directory)}
        unknown = sorted(wanted_lower - matched)
        if unknown:
            raise workspace_config.WorkspaceError(
                "no handoff directory matches --queue "
                + ", ".join(unknown)
                + ". Known: "
                + ", ".join(sorted(alias_of[directory] for directory in directories))
            )
        directories = selected

    ledgers = _ledger_totals(data_root)

    # --- one entry per canonical identity ------------------------------------
    by_identity: dict[tuple[str, str], list[Path]] = {}
    for directory in directories:
        for path in sorted((handoff_root / directory).glob("*.md")):
            fields = _frontmatter(path)
            # `prompt_path` is the identity WHEN IT IS ONE. A manual-work handoff
            # carries `null`, or a sentence saying there was no prompt file, and
            # grouping every such handoff under one key would collapse twenty
            # separate pieces of work into a single "duplicate identity" warning
            # and drop them all from the record. The handoff's own filename is
            # the identity in that case, which is exactly what it is.
            stem = _identity(fields.get("prompt_path")) or path.stem
            by_identity.setdefault((directory, stem), []).append(path)

    entries: list[dict] = []
    for (directory, stem), paths in sorted(by_identity.items()):
        where = f"{directory}/{stem}"
        canonical = [path for path in paths if path.stem == stem]
        if len(paths) > 1 and len(canonical) != 1:
            warnings.append(
                f"{where}: {len(paths)} handoffs claim this prompt "
                f"({', '.join(sorted(path.name for path in paths))}) and none is "
                "canonically named — excluded from the total"
            )
            entries.append(
                {
                    "directory": directory,
                    "alias": alias_of.get(directory, directory),
                    "prompt": stem + ".md",
                    "status": "ambiguous",
                    "seconds": None,
                    "attempts": None,
                    "started_at": None,
                    "finished_at": None,
                    "measurement": "unknown",
                    "counted": False,
                }
            )
            continue
        path = canonical[0] if canonical else paths[0]
        if len(paths) > 1:
            warnings.append(
                f"{where}: {len(paths)} handoff files share this identity; using the "
                f"canonically named {path.name}"
            )
        fields = _frontmatter(path)
        status = fields.get("status") or "unknown"
        seconds = _seconds(fields.get("execution_seconds"), warnings, where)
        attempts_raw = fields.get("execution_attempts")
        attempts: int | None
        try:
            attempts = int(attempts_raw) if attempts_raw not in (None, "", "null") else None
        except (TypeError, ValueError):
            warnings.append(f"{where}: execution_attempts is not an integer ({attempts_raw!r})")
            attempts = None
        started = fields.get("execution_started_at") or None
        finished = fields.get("execution_finished_at") or None
        if finished in ("null", ""):
            finished = None
        measurement = fields.get("execution_measurement") or ("unknown" if seconds is None else "runner")
        if measurement not in execution_time.MEASUREMENTS:
            warnings.append(
                f"{where}: execution_measurement {measurement!r} is not one of "
                f"{', '.join(execution_time.MEASUREMENTS)}"
            )
        if seconds is not None and started and finished:
            begin, end = execution_time.parse(started), execution_time.parse(finished)
            if begin and end and end < begin:
                warnings.append(
                    f"{where}: execution_finished_at precedes execution_started_at; "
                    "the explicit execution_seconds is retained"
                )

        # THE HANDOFF WINS. The ledger is consulted only when the handoff carries
        # no timing at all, which is what makes double-counting impossible rather
        # than merely unlikely.
        if seconds is None:
            ledger = ledgers.get(stem + ".md")
            if ledger and ledger["attempts"]:
                seconds = int(ledger["seconds"])
                attempts = attempts or ledger["attempts"]
                started = started or (min(ledger["starts"]) if ledger["starts"] else None)
                finished = finished or (max(ledger["finishes"]) if ledger["finishes"] else None)
                measurement = "runner"

        entries.append(
            {
                "directory": directory,
                "alias": alias_of.get(directory, directory),
                "prompt": path.stem + ".md",
                "status": status,
                "seconds": seconds,
                "attempts": attempts,
                "started_at": started,
                "finished_at": finished,
                "measurement": measurement if seconds is not None else "unknown",
                "counted": seconds is not None,
            }
        )

    def order(entry: dict) -> tuple:
        started = execution_time.parse(entry["started_at"])
        finished = execution_time.parse(entry["finished_at"])
        # Sort key, in the documented order, with a deterministic fallback so two
        # undated entries never swap places between runs.
        return (
            0 if started else (1 if finished else 2),
            (started or finished or execution_time.parse("1970-01-01T00:00:00Z")),
            entry["directory"],
            entry["prompt"],
        )

    entries.sort(key=order)

    completed_seconds = sum(
        entry["seconds"] for entry in entries
        if entry["counted"] and entry["status"] in COMPLETED_STATUSES
    )
    partial_seconds = sum(
        entry["seconds"] for entry in entries
        if entry["counted"] and entry["status"] in PARTIAL_STATUSES
    )
    other_seconds = sum(
        entry["seconds"] for entry in entries
        if entry["counted"] and entry["status"] not in COMPLETED_STATUSES | PARTIAL_STATUSES
    )
    return {
        "schema": "run-sequence.history/1",
        "directories": directories,
        "entries": entries,
        "warnings": warnings,
        "totals": {
            "measured_prompts": sum(1 for entry in entries if entry["counted"]),
            "unknown_duration": sum(1 for entry in entries if not entry["counted"]),
            "partial_or_in_progress": sum(
                1 for entry in entries if entry["status"] in PARTIAL_STATUSES
            ),
            "completed_seconds": completed_seconds,
            "partial_seconds": partial_seconds,
            "other_seconds": other_seconds,
            "combined_seconds": completed_seconds + partial_seconds + other_seconds,
        },
    }


def human_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m"


def _short(stamp: str | None) -> str:
    moment = execution_time.parse(stamp)
    return moment.strftime("%Y-%m-%d %H:%M") if moment else "-"


def render_human(report: dict) -> str:
    out: list[str] = []
    out.append("run-sequence.sh --history — READ-ONLY EXECUTION HISTORY")
    out.append("")
    out.append(
        "Execution time is supervised agent WALL CLOCK — not CPU time, not tokens,"
    )
    out.append("not billed time. The total is a SUM of per-prompt seconds, so prompts run")
    out.append("in parallel each contribute their full duration.")
    out.append("")
    # The REPO column is sized to the aliases actually present, not to a fixed
    # width. The source toolkit's aliases were three or four characters, so a
    # hardcoded 8 was invisible there; a configured alias is whatever the operator
    # called it, and a truncated one ("example-") is a column that lies.
    alias_width = max(
        [len("REPO")] + [len(entry["alias"]) for entry in report["entries"]]
    ) + 1
    header = (
        f"{'REPO':<{alias_width}}{'PROMPT':<62}{'STATUS':<12}{'TIME':>9}  "
        f"{'ATT':>3}  {'STARTED':<17}{'FINISHED':<17}MEASUREMENT"
    )
    out.append(header)
    out.append("-" * len(header))
    for entry in report["entries"]:
        prompt = entry["prompt"]
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        out.append(
            f"{entry['alias']:<{alias_width}}{prompt:<62}{entry['status'][:11]:<12}"
            f"{human_duration(entry['seconds']):>9}  "
            f"{(str(entry['attempts']) if entry['attempts'] else '-'):>3}  "
            f"{_short(entry['started_at']):<17}{_short(entry['finished_at']):<17}"
            f"{entry['measurement']}"
        )
    totals = report["totals"]
    out.append("")
    out.append(f"  Measured prompts:      {totals['measured_prompts']:>6}")
    out.append(f"  Unknown-duration:      {totals['unknown_duration']:>6}")
    out.append(f"  Partial/in-progress:   {totals['partial_or_in_progress']:>6}")
    out.append("")
    out.append(
        f"  Completed prompt time:   {human_duration(totals['completed_seconds'])}"
        f"  ({totals['completed_seconds']}s)"
    )
    out.append(
        f"  Partial/in-progress time:{human_duration(totals['partial_seconds']):>8}"
        f"  ({totals['partial_seconds']}s)"
    )
    if totals["other_seconds"]:
        out.append(
            f"  Other measured time:     {human_duration(totals['other_seconds'])}"
            f"  ({totals['other_seconds']}s)"
        )
    out.append(
        f"  Combined measured time:  {human_duration(totals['combined_seconds'])}"
        f"  ({totals['combined_seconds']}s)"
    )
    out.append("")
    out.append(
        "  Unknown-duration handoffs are counted above and EXCLUDED from the total;"
    )
    out.append("  they are not treated as zero seconds.")
    if report["warnings"]:
        out.append("")
        out.append("  WARNINGS")
        for warning in report["warnings"]:
            out.append(f"    {warning}")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="an explicit workspace.json")
    parser.add_argument("--queue", action="append", default=[], metavar="ALIAS")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args()

    try:
        report = collect(workspace_config.load(args.config), args.queue)
    except workspace_config.WorkspaceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
