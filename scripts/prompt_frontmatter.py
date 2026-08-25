#!/usr/bin/env python3
"""The ONE decoder for the Markdown frontmatter this workspace writes.

WHY THIS FILE EXISTS
--------------------
Six independent hand parsers read frontmatter in the source toolkit and they did
not agree. On one prompt file — eleven lines a Markdown editor had reformatted —
`sequence_plan.parse_mutation_targets` read a mutation target,
`branch_policy.declared_targets` read a repository, and
`resolve_next_prompt.extract_requires` read `[]`. The prompt declared a
prerequisite; selection could not see it.

An empty dependency list and an unreadable one are the same value in a parser
that returns `[]` for both, and only one of them is safe. Dependency selection
is a safety boundary, so it gets one decoder, one grammar, and a named error
where it used to get silence.

WHY NOT PyYAML
--------------
It is importable on the machine this was written on, and it still cannot be
the decoder:

  * the grammar this workspace must READ is a documented SUPERSET of YAML. A
    Markdown editor reformats `- item` into `* item`, and in YAML `*` opens an
    alias node — so PyYAML raises on the exact file this task exists to read.
    A decoder that rejects the corpus is not a decoder.
  * `branch_policy.py` is invoked from shell (`run-agent.sh`,
    `run-sequence.sh`) on machines with nothing installed but python3. Its own
    docstring says so. The branch preflight must not acquire a dependency.

So: a small internal parser whose accepted grammar is written down here and
asserted in `tests/test_prompt_frontmatter.py`, per the prompt's own fallback
clause. What is NOT tolerated is now as explicit as what is.

THE ACCEPTED GRAMMAR
--------------------
::

    document      := "---" EOL body "---" (EOL | EOF)
    body          := line*
    line          := blank | comment | key-line | item-line
    blank         := WS*
    comment       := WS* "#" ANY*
    key-line      := WS* key WS* ":" (WS* value)?
    item-line     := WS* bullet (WS+ item)?
    bullet        := "-" | "*"
    key           := [A-Za-z_][A-Za-z0-9_-]*
    value         := flow-seq | scalar
    flow-seq      := "[" (scalar ("," scalar)*)? "]"
    item          := key ":" WS* scalar | scalar
    scalar        := quoted-scalar | bare-scalar

Semantics, each one of which exists because a real file needed it:

  * **`-` and `*` are the same bullet.** The whole point.
  * **Indentation is advisory for a key, structural for a mapping.** A
    reformatter indents the key that follows a list (`  requires:` two spaces
    in under a de-indented `* item`), so a key-line CLOSES an open block list
    at any indentation. A nested mapping is opened only by a value-less key
    whose *next meaningful line is a more-indented key-line* — which is how
    the handoff schema's `block:` is written and how it stays readable.
  * **A mapping list item continues while it is more indented than its own
    bullet.** `- repo: X` / `  prompt: Y` is one cross-repository requirement,
    not a requirement literally named "repo: X". That bug shipped once.
  * **Blank lines and full-line comments never close a container.** A
    reformatter inserts blank lines inside a block list.
  * **An inline `#` comment is stripped from an UNQUOTED value, for prompts
    only.** `- 20260803_07_Task   # the dirty-worktree one` is a prerequisite
    on `20260803_07_Task`,
    and the silent alternative is a requirement that can never be satisfied.
    Handoff headers are parsed with ``inline_comments=False`` because
    `agent_task.render_block` writes `summary:` as an unquoted human sentence
    and a sentence may contain a `#`. One parser, two declared document
    classes — not two parsers.

WHAT IS AN ERROR
----------------
Every one of these raises `FrontmatterError`, which carries the path, the line
number and a stable `code`. None of them may EVER be answered with an empty
list:

  ``unterminated_frontmatter``  opening `---` with no closing `---`
  ``unparsable_line``           a line that is not blank/comment/key/item
  ``unexpected_list_item``      a bullet with no list open above it
  ``empty_list_item``           a bullet with nothing after it
  ``duplicate_key``             one mapping sets a key twice, differently
  ``tab_indent``                a tab in leading whitespace (YAML forbids it,
                                and its width is a guess)

Absent frontmatter is NOT an error: a prompt written before the convention has
none, and its prerequisites are read from prose. An absent FIELD is not an
error either — `field_list()` returns None for "not declared" and `[]` for
"declared empty", and the difference decides parallelism in `sequence_plan`.

Usage (also a CLI, for the shell callers):

    prompt_frontmatter.py get FILE FIELD [--handoff]
    prompt_frontmatter.py json FILE [--handoff]
    prompt_frontmatter.py lint [PATH ...] [--data-root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: Opening/closing fence. `\s*` after each fence absorbs a stray carriage
#: return for a caller that read the file in binary and decoded it itself;
#: Python text mode has already translated CRLF before this is ever reached.
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
OPENING_FENCE_RE = re.compile(r"\A---[ \t]*(?:\n|\Z)")

KEY_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?P<rest>.*)$")
ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<bullet>[-*])(?P<rest>[ \t].*|[ \t]*)$")
COMMENT_RE = re.compile(r"^[ \t]*#")
INLINE_COMMENT_RE = re.compile(r"(?:^|[ \t])#.*$")

#: Sentinel for `key:` with nothing under it. `field_list` reads it as `[]`
#: and `field_scalar` as `""` — an explicitly empty declaration either way,
#: which is a different fact from an absent key and is kept different.
EMPTY = object()


class FrontmatterError(ValueError):
    """Malformed or ambiguous frontmatter, named and located.

    It must never be caught and turned back into an empty list. The whole
    point of this class is that "I could not read this" stops being spelled
    the same way as "there is nothing to read".
    """

    def __init__(self, code: str, message: str, *, path: Path | None = None, line: int | None = None):
        self.code = code
        self.path = path
        self.line = line
        where = str(path) if path else "<text>"
        if line is not None:
            where = f"{where}:{line}"
        super().__init__(f"{where}: {code}: {message}")


def _strip_quotes(value: str) -> tuple[str, bool]:
    """Return the scalar with matching surrounding quotes removed, and whether
    it WAS quoted (a quoted scalar keeps its `#`)."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1], True
    return value, False


def _scalar(raw: str, inline_comments: bool) -> str:
    value, quoted = _strip_quotes(raw)
    if quoted or not inline_comments:
        return value
    return INLINE_COMMENT_RE.sub("", value).strip()


def _flow_sequence(raw: str, inline_comments: bool) -> list[str] | None:
    value = raw.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return None
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [item for item in (_scalar(part, inline_comments) for part in inner.split(",")) if item]


def _mapping_item(raw: str, inline_comments: bool) -> tuple[str, str] | None:
    """`repo: api` inside a list item -> ("repo", "api")."""
    match = re.match(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?P<rest>.*)$", raw)
    if not match:
        return None
    return match.group("key"), _scalar(match.group("rest"), inline_comments)


class Frontmatter:
    """A parsed frontmatter block.

    `fields` is the top-level mapping. A value is one of: a string, a list
    (whose items are strings or single-level dicts), a dict (a nested
    mapping), or `EMPTY`.
    """

    def __init__(self, fields: dict[str, Any], *, path: Path | None = None, present: bool = True):
        self.fields = fields
        self.path = path
        self.present = present

    # -- accessors: the difference between "absent" and "empty" is preserved --
    def has(self, name: str) -> bool:
        return name in self.fields

    def field_scalar(self, name: str) -> str | None:
        """The scalar value of `name`, or None when it is absent.

        A key that opened a list or a mapping has no scalar value and returns
        None; `key:` with nothing under it is the empty string.
        """
        if name not in self.fields:
            return None
        value = self.fields[name]
        if value is EMPTY:
            return ""
        if isinstance(value, str):
            return value
        return None

    def field_list(self, name: str) -> list[Any] | None:
        """The list value of `name`: None when absent, `[]` when empty.

        A scalar is returned as a one-item list, which is how
        `requires: SOME-PROMPT` has always been read.
        """
        if name not in self.fields:
            return None
        value = self.fields[name]
        if value is EMPTY:
            return []
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            return [value]
        return [value] if value else []

    def field_strings(self, name: str) -> list[str] | None:
        """`field_list` with mapping items dropped — for plain string lists."""
        items = self.field_list(name)
        if items is None:
            return None
        return [item for item in items if isinstance(item, str) and item]

    def field_mapping(self, name: str) -> dict[str, Any]:
        """A nested mapping (the handoff schema's `block:`), or `{}`."""
        value = self.fields.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def scalars(self) -> dict[str, str]:
        """Every top-level scalar, flattened — the shape the old
        `_parse_frontmatter` helpers returned, for the readers that only ever
        wanted `status` or `prompt_path`."""
        out: dict[str, str] = {}
        for key, value in self.fields.items():
            if value is EMPTY:
                out[key] = ""
            elif isinstance(value, str):
                out[key] = value
        return out

    def to_json(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if value is EMPTY:
                return None
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        return {key: convert(value) for key, value in self.fields.items()}


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------
def parse(text: str, *, path: Path | None = None, inline_comments: bool = True) -> Frontmatter:
    """Decode a frontmatter block. Raises `FrontmatterError` when malformed.

    `inline_comments=False` for machine-owned handoff headers, whose unquoted
    values are human sentences that may contain a `#`.
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        if OPENING_FENCE_RE.match(text):
            raise FrontmatterError(
                "unterminated_frontmatter",
                "the block opens with `---` and never closes with `---` on its own line",
                path=path,
                line=1,
            )
        return Frontmatter({}, path=path, present=False)

    root: dict[str, Any] = {}
    # Container stack. A mapping frame owns an indent and a dict; a list frame
    # owns the dict+key it will be stored under and the bullet indent of its
    # most recent item (for mapping-item continuation).
    maps: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    open_list: dict[str, Any] | None = None
    pending: tuple[int, dict[str, Any], str] | None = None  # (indent, owner, key)

    def set_key(owner: dict[str, Any], key: str, value: Any, line_no: int) -> None:
        if key in owner and owner[key] != value:
            raise FrontmatterError(
                "duplicate_key",
                f"`{key}` is set twice in the same mapping with different values; "
                "a reader cannot tell which one was meant",
                path=path,
                line=line_no,
            )
        owner[key] = value

    def close_list() -> None:
        nonlocal open_list
        open_list = None

    def settle_pending(line_no: int) -> None:
        """A value-less key that nothing indented followed is explicitly empty."""
        nonlocal pending
        if pending is not None:
            _, owner, key = pending
            set_key(owner, key, EMPTY, line_no)
            pending = None

    line_no = 1
    for offset, line in enumerate(match.group(1).splitlines()):
        line_no = offset + 2  # +1 for the opening fence, +1 for 1-based
        if not line.strip():
            continue
        if COMMENT_RE.match(line):
            continue
        leading = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in leading:
            raise FrontmatterError(
                "tab_indent",
                "a tab in leading whitespace: YAML forbids it and its width is a guess",
                path=path,
                line=line_no,
            )

        item_match = ITEM_RE.match(line)
        if item_match:
            indent = len(item_match.group("indent"))
            body = item_match.group("rest").strip()
            # A bullet is what turns the pending key into a list.
            if pending is not None:
                _, owner, key = pending
                open_list = {"owner": owner, "key": key, "items": [], "bullet_indent": indent}
                set_key(owner, key, open_list["items"], line_no)
                pending = None
            if open_list is None:
                raise FrontmatterError(
                    "unexpected_list_item",
                    f"`{line.strip()}` is a list item, but no key above it opened a list",
                    path=path,
                    line=line_no,
                )
            if not body:
                raise FrontmatterError(
                    "empty_list_item",
                    "a bullet with nothing after it; an unnamed entry cannot be resolved",
                    path=path,
                    line=line_no,
                )
            open_list["bullet_indent"] = indent
            mapping = _mapping_item(body, inline_comments)
            if mapping is not None:
                open_list["items"].append({mapping[0]: mapping[1]})
            else:
                open_list["items"].append(_scalar(body, inline_comments))
            continue

        key_match = KEY_RE.match(line)
        if key_match:
            indent = len(key_match.group("indent"))
            key = key_match.group("key")
            rest = key_match.group("rest")

            # (a) continuation of a mapping LIST ITEM: `- repo: X` / `  prompt: Y`.
            if (
                open_list is not None
                and open_list["items"]
                and isinstance(open_list["items"][-1], dict)
                and indent > open_list["bullet_indent"]
            ):
                open_list["items"][-1][key] = _scalar(rest, inline_comments)
                continue

            # (b) any other key closes an open list, whatever its indentation:
            #     a reformatter indents the key that follows a de-indented list.
            close_list()

            # (c) a value-less key followed by a MORE-INDENTED key opens a
            #     nested mapping; that is the only thing that opens one.
            if pending is not None:
                pending_indent, owner, pending_key = pending
                if indent > pending_indent:
                    nested: dict[str, Any] = {}
                    set_key(owner, pending_key, nested, line_no)
                    maps.append((indent, nested))
                    pending = None
                else:
                    settle_pending(line_no)

            while len(maps) > 1 and indent < maps[-1][0]:
                maps.pop()
            owner = maps[-1][1]

            value = rest.strip()
            if not value:
                pending = (indent, owner, key)
                continue
            flow = _flow_sequence(value, inline_comments)
            if flow is not None:
                set_key(owner, key, flow, line_no)
            else:
                set_key(owner, key, _scalar(value, inline_comments), line_no)
            continue

        raise FrontmatterError(
            "unparsable_line",
            f"`{line.strip()}` is neither a `key:` line, a `-`/`*` list item, "
            "a comment nor blank",
            path=path,
            line=line_no,
        )

    settle_pending(line_no)
    return Frontmatter(root, path=path, present=True)


def read_text(path: Path) -> str:
    """Python text mode, deliberately: universal newlines translate the CRLF
    that a Markdown editor leaves behind before any regex sees it."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def parse_file(path: Path, *, inline_comments: bool = True) -> Frontmatter:
    return parse(read_text(path), path=Path(path), inline_comments=inline_comments)


def parse_prompt(path: Path) -> Frontmatter:
    """A hand/LLM-authored prompt: inline `#` comments are comments."""
    return parse_file(path, inline_comments=True)


def parse_handoff(path: Path) -> Frontmatter:
    """A machine-owned handoff header: an unquoted value is a human sentence."""
    return parse_file(path, inline_comments=False)


def parse_handoff_tolerant(path: Path) -> Frontmatter:
    """A handoff header that must never be able to stop prompt SELECTION.

    A handoff is read for two secondary purposes (resume, and the
    completed-but-not-moved guard) and is not a dependency edge. A malformed
    one is reported by `lint`, not by refusing to pick the next prompt — the
    file it describes may not even be the candidate. Prompts get no such
    tolerance, which is the asymmetry this pair of functions exists to make
    visible.
    """
    try:
        return parse_handoff(path)
    except FrontmatterError:
        return Frontmatter({}, path=Path(path), present=False)


# ---------------------------------------------------------------------------
# lint — read-only, for CI/preflight
# ---------------------------------------------------------------------------
def lint_paths(paths: list[Path]) -> list[dict]:
    findings: list[dict] = []
    for path in paths:
        handoff = "handoffs" in path.parts
        try:
            parse_file(path, inline_comments=not handoff)
        except FrontmatterError as error:
            findings.append(
                {
                    "path": str(path),
                    "code": error.code,
                    "line": error.line,
                    "message": str(error),
                }
            )
        except OSError as error:  # unreadable file is a finding, not a crash
            findings.append(
                {"path": str(path), "code": "unreadable", "line": None, "message": str(error)}
            )
    return findings


def workspace_markdown(data_root: Path) -> list[Path]:
    """Every prompt (queued, done, blocked) and every handoff, sorted."""
    found: list[Path] = []
    for kind in ("prompts", "handoffs"):
        base = data_root / "LLM" / kind
        if not base.is_dir():
            continue
        found.extend(sorted(p for p in base.rglob("*.md") if p.is_file()))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Decode this workspace's Markdown frontmatter.")
    sub = parser.add_subparsers(dest="command", required=True)

    get_parser = sub.add_parser("get", help="print one top-level scalar field")
    get_parser.add_argument("path", type=Path)
    get_parser.add_argument("field")
    get_parser.add_argument(
        "--handoff",
        action="store_true",
        help="machine-owned handoff header: do not strip inline `#` comments",
    )

    json_parser = sub.add_parser("json", help="print the whole block as JSON")
    json_parser.add_argument("path", type=Path)
    json_parser.add_argument("--handoff", action="store_true")

    lint_parser = sub.add_parser("lint", help="fail on malformed frontmatter (read-only)")
    lint_parser.add_argument("paths", nargs="*", type=Path)
    lint_parser.add_argument("--data-root", type=Path, help="lint every prompt and handoff under it")
    lint_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command in ("get", "json"):
        try:
            block = parse_file(args.path, inline_comments=not args.handoff)
        except FrontmatterError as error:
            print(str(error), file=sys.stderr)
            return 2
        if args.command == "json":
            print(json.dumps(block.to_json(), indent=2, sort_keys=True))
            return 0
        value = block.field_scalar(args.field)
        if value is None:
            items = block.field_strings(args.field)
            value = "\n".join(items) if items else ""
        print(value)
        return 0

    paths = list(args.paths)
    if args.data_root:
        paths.extend(workspace_markdown(args.data_root))
    if not paths:
        print("lint: nothing to check (pass paths or --data-root)", file=sys.stderr)
        return 2
    findings = lint_paths(paths)
    if args.json:
        print(json.dumps({"checked": len(paths), "findings": findings}, indent=2, sort_keys=True))
    else:
        for finding in findings:
            print(finding["message"], file=sys.stderr)
        print(f"lint: checked {len(paths)} file(s), {len(findings)} malformed")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
