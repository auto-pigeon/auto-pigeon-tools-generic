#!/usr/bin/env python3
"""Render `claude -p --output-format stream-json` as live terminal progress.

Plain `claude -p` prints nothing at all until the entire task finishes. For a
task that runs for many minutes that is indistinguishable from a hang, and an
unattended `run-sequence.sh` run looks dead from the moment it starts. This
reads the streaming JSON transcript on stdin and prints one short line per
assistant message and per tool call, as they arrive, so a healthy run visibly
progresses.

It is deliberately forgiving: any line that is not valid JSON, or is a shape it
does not recognize, is passed through rather than swallowed, so this can never
hide an error message coming from the agent CLI.

Usage:  claude -p --verbose --output-format stream-json ... | stream_progress.py
"""

from __future__ import annotations

import json
import sys
import time

MAX_LINE = 110


def truncate(text: str, limit: int = MAX_LINE) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def tool_summary(name: str, params: dict) -> str:
    """One-line description of a tool call, favouring the identifying argument."""
    if not isinstance(params, dict):
        return name
    for key in ("file_path", "path", "pattern", "command", "url", "prompt", "query"):
        if key in params and params[key]:
            return f"{name} {truncate(params[key], 80)}"
    return name


def main() -> int:
    start = time.monotonic()

    def emit(marker: str, text: str) -> None:
        elapsed = time.monotonic() - start
        print(f"    [{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}] {marker} {text}", flush=True)

    exit_code = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Not JSON — almost certainly a real message from the CLI itself
            # (an error, a login prompt). Never hide it.
            print(line, flush=True)
            continue

        kind = event.get("type")

        if kind == "system" and event.get("subtype") == "init":
            emit("·", f"session started (model {event.get('model', 'unknown')})")

        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    emit("»", truncate(block["text"]))
                elif block.get("type") == "tool_use":
                    emit("·", tool_summary(block.get("name", "tool"), block.get("input", {})))

        elif kind == "result":
            subtype = event.get("subtype", "")
            turns = event.get("num_turns")
            cost = event.get("total_cost_usd")
            detail = f"result: {subtype}"
            if turns is not None:
                detail += f", {turns} turns"
            if isinstance(cost, (int, float)):
                detail += f", ${cost:.2f}"
            emit("✓" if subtype == "success" else "✗", detail)
            if subtype != "success":
                exit_code = 1
                if event.get("result"):
                    print(truncate(event["result"], 500), flush=True)

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
