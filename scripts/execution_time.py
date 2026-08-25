#!/usr/bin/env python3
"""Execution-time accounting: the runner measures, the handoff records.

WHAT "EXECUTION TIME" MEANS HERE, EXACTLY
-----------------------------------------
    execution time = the sum of supervised attempt wall-clock durations
                     for one prompt

An attempt starts when the runner starts the agent process and ends when that
process has stopped. Everything the agent does inside that window counts —
thinking, tool calls, its interactive turn — because from the runner's chair
that is one process running.

What is deliberately EXCLUDED: the gap between two attempts, the gap between two
invocations of `run-sequence.sh`, anything after the runner closed the idle TUI,
and an operator's own time before a manual resume. None of those has an agent
running in it.

WHAT IT IS NOT. It is not CPU time — a mostly-idle attempt waiting on a slow
`npm install` costs the same here as a busy one. It is not token usage and it is
not billed time; nothing in this module can see a token. It is an approximation
of consumed agent/runtime wall clock, and the only honest way to describe a
number produced here is by that definition.

WHO IS THE AUTHORITY. The runner, never the model. The runner already knows when
it starts an attempt, when a Stop arrives, when a rollover happens, when an exit
status comes back and when completion is validated — so timing is two
`datetime.now()` calls at lifecycle boundaries per attempt and nothing else. No
polling loop, no sampling, no clock reading inside the agent's turn. The overhead
is a rounding error on a multi-hour prompt, which is the point: an instrument
that changed what it measured would be worse than none.

IDEMPOTENCE, WHICH IS THE PROPERTY THAT ACTUALLY BITES
------------------------------------------------------
The runner refreshes the handoff several times per prompt — before each attempt,
after each rollover, at completion. If timing were ACCUMULATED into the handoff
each time, one prompt's seconds would be added two or three times and the total
would be quietly wrong in the direction nobody checks.

So the attempt records under the run directory are the ledger, and the handoff
field is a PROJECTION of them: every write recomputes the sum from the ledger and
replaces the field. Writing it twice produces the same number as writing it once.

Attempt records live in the runner-owned run directory, written the moment an
attempt ends, so a crash costs the current attempt's tail and nothing earlier.

Usage:
    execution_time.py record  --run-dir DIR --repo ALIAS --prompt NAME \\
                              --attempt N --started-at TS [--finished-at TS] \\
                              --outcome WORD
    execution_time.py apply   --run-dir DIR --repo ALIAS --prompt NAME \\
                              --handoff PATH [--final]
    execution_time.py show    --run-dir DIR --repo ALIAS --prompt NAME
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prompt_frontmatter  # noqa: E402

SCHEMA = "run-sequence.timing/1"

# The five fields a handoff carries. Fixed names, fixed order, and the order is
# what keeps a re-write from producing a spurious diff.
FIELD_ORDER = (
    "execution_seconds",
    "execution_attempts",
    "execution_started_at",
    "execution_finished_at",
    "execution_measurement",
)
MEASUREMENTS = ("runner", "checkpoint_estimate", "agent_reported", "unknown")

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.DOTALL)


def utc_now() -> str:
    return stamp(datetime.now(timezone.utc))


def stamp(moment: datetime) -> str:
    """RFC3339, UTC, with `Z` rather than `+00:00`.

    Both spellings are legal RFC3339 and mean the same instant; one of them is
    what every other timestamp in this workspace's handoffs looks like, and a
    field that is sometimes one and sometimes the other is a field every reader
    has to write two parsers for.
    """
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(value: str | None) -> datetime | None:
    if not value or value in ("null", "~", "none"):
        return None
    text = value.strip().strip("\"'")
    if not text or text == "null":
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
def ledger_path(run_dir: Path, repo: str, prompt: str) -> Path:
    return run_dir / "timing" / repo.upper() / (Path(prompt).stem + ".attempts.json")


def _read_ledger(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "attempts": []}
    if not isinstance(value, dict) or not isinstance(value.get("attempts"), list):
        return {"schema": SCHEMA, "attempts": []}
    return value


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=str(path.parent), prefix=".timing-", suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(staged, path)


def record_attempt(
    run_dir: Path,
    repo: str,
    prompt: str,
    attempt: int,
    started_at: str,
    finished_at: str | None,
    outcome: str,
) -> dict:
    """Persist one attempt. Re-recording the same attempt REPLACES it.

    Replacement, not append, is what makes the ledger safe to write from every
    lifecycle boundary: a rollover writes attempt 3, the completion path may
    write attempt 3 again with a longer tail, and the answer is still one
    attempt 3.
    """
    path = ledger_path(run_dir, repo, prompt)
    payload = _read_ledger(path)
    start = parse(started_at)
    finish = parse(finished_at)
    elapsed = None
    if start is not None and finish is not None:
        elapsed = int(round((finish - start).total_seconds()))
        if elapsed < 0:
            # A clock that went backwards is evidence of nothing. Keep the
            # record, refuse the number.
            elapsed = None
    entry = {
        "prompt": Path(prompt).name,
        "attempt": int(attempt),
        "started_at": stamp(start) if start else None,
        "finished_at": stamp(finish) if finish else None,
        "elapsed_seconds": elapsed,
        "outcome": outcome,
    }
    attempts = [item for item in payload["attempts"] if item.get("attempt") != int(attempt)]
    attempts.append(entry)
    payload["schema"] = SCHEMA
    payload["attempts"] = sorted(attempts, key=lambda item: item.get("attempt") or 0)
    _write_atomic(path, payload)
    return entry


def totals(run_dir: Path, repo: str, prompt: str) -> dict:
    """The projection: what the handoff fields should say, right now.

    An UNFINISHED attempt contributes nothing to the sum and does not make the
    measurement an estimate — `finished_at` simply stays null until it lands.
    An attempt with a start but no finish that will never get one is the crash
    case, and `--final` below is what turns it into a `checkpoint_estimate`
    rather than a silently missing chunk.
    """
    payload = _read_ledger(ledger_path(run_dir, repo, prompt))
    attempts = payload["attempts"]
    seconds = sum(item.get("elapsed_seconds") or 0 for item in attempts)
    starts = [parse(item.get("started_at")) for item in attempts]
    starts = [item for item in starts if item is not None]
    finishes = [parse(item.get("finished_at")) for item in attempts]
    finishes = [item for item in finishes if item is not None]
    unfinished = [item for item in attempts if item.get("started_at") and not item.get("finished_at")]
    return {
        "execution_seconds": int(seconds),
        "execution_attempts": len(attempts),
        "execution_started_at": stamp(min(starts)) if starts else None,
        "execution_finished_at": stamp(max(finishes)) if finishes else None,
        "unfinished_attempts": len(unfinished),
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# The handoff projection
# ---------------------------------------------------------------------------
def read_fields(handoff: Path) -> dict[str, str]:
    """The five `execution_*` fields already in a handoff header.

    Decoded by `prompt_frontmatter`, which reads TOP-LEVEL
    keys only — the line splitter this replaces would have taken an indented
    `execution_seconds:` nested inside `block:` for the real one. WRITING these
    fields is still the line-level rewrite below, deliberately: it runs against
    files an agent is editing at the same time, and the smallest possible edit
    is the only safe one.
    """
    if not handoff.is_file():
        return {}
    scalars = prompt_frontmatter.parse_handoff_tolerant(handoff).scalars()
    return {key: value for key, value in scalars.items() if key in FIELD_ORDER}


def _render(fields: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key in FIELD_ORDER:
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            lines.append(f"{key}: null")
        elif key in ("execution_started_at", "execution_finished_at"):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    return lines


def write_fields(handoff: Path, fields: dict[str, object]) -> bool:
    """Replace the five timing lines in a handoff's frontmatter, in place.

    Nothing else in the file is touched: no other key is reordered, no body line
    is rewritten, and a handoff with no frontmatter at all is left exactly as it
    was rather than being given one. This runs against files an agent is also
    editing, so the smallest possible edit is the only safe one.
    """
    if not handoff.is_file():
        return False
    text = handoff.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    body_start = match.end()
    kept = [
        line
        for line in match.group(1).splitlines()
        if line.partition(":")[0].strip() not in FIELD_ORDER
    ]
    rendered = "\n".join(kept + _render(fields))
    updated = "---\n" + rendered + "\n---\n" + text[body_start:]
    if updated == text:
        return True
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=str(handoff.parent), prefix=".handoff-", suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(updated)
    os.replace(staged, handoff)
    return True


def apply(run_dir: Path, repo: str, prompt: str, handoff: Path, final: bool) -> dict:
    summary = totals(run_dir, repo, prompt)
    measurement = "runner"
    if summary["execution_attempts"] == 0:
        measurement = "unknown"
    elif final and summary["unfinished_attempts"]:
        # The runner started an attempt it never saw end — a crash, a kill, a
        # session restored by hand. The seconds it did measure are real; the
        # missing tail is not invented, it is DECLARED missing.
        measurement = "checkpoint_estimate"
    fields: dict[str, object] = {
        "execution_seconds": summary["execution_seconds"],
        "execution_attempts": summary["execution_attempts"],
        "execution_started_at": summary["execution_started_at"],
        "execution_finished_at": summary["execution_finished_at"] if final else None,
        "execution_measurement": measurement,
    }
    written = write_fields(handoff, fields)
    return {"written": written, "fields": fields, **summary}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run-dir", required=True, type=Path)
    common.add_argument("--repo", required=True)
    common.add_argument("--prompt", required=True)

    record = sub.add_parser("record", parents=[common])
    record.add_argument("--attempt", required=True, type=int)
    record.add_argument("--started-at", required=True)
    record.add_argument("--finished-at", default=None)
    record.add_argument("--outcome", default="unknown")

    applied = sub.add_parser("apply", parents=[common])
    applied.add_argument("--handoff", required=True, type=Path)
    applied.add_argument("--final", action="store_true")

    sub.add_parser("show", parents=[common])
    # One place produces this workspace's timestamp spelling, so a shell caller
    # never has to get `date -u +%Y-%m-%dT%H:%M:%SZ` right a second time.
    sub.add_parser("now")

    args = parser.parse_args()
    if args.command == "now":
        print(utc_now())
        return 0
    if args.command == "record":
        entry = record_attempt(
            args.run_dir, args.repo, args.prompt, args.attempt,
            args.started_at, args.finished_at, args.outcome,
        )
        print(json.dumps(entry, sort_keys=True))
        return 0
    if args.command == "apply":
        result = apply(args.run_dir, args.repo, args.prompt, args.handoff, args.final)
        print(json.dumps(result["fields"], sort_keys=True))
        return 0
    print(json.dumps(totals(args.run_dir, args.repo, args.prompt), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
