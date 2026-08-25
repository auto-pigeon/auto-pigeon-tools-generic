#!/usr/bin/env bash
set -uo pipefail

# Initialization through the operator-facing script, in a disposable father
# directory. The Python cases in tests/test_workspace_init.py cover the planner's
# decisions; this covers the thing an operator actually types, including the
# property that the default writes nothing.

TOOLKIT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

passed=0
failed=0
ok() { printf '  ok    %s\n' "$1"; passed=$((passed + 1)); }
no() { printf '  FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; failed=$((failed + 1)); }
check() { if [[ "$2" == "$3" ]]; then ok "$1"; else no "$1" "expected [$2], got [$3]"; fi; }
contains() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "missing [$3]"; fi; }

git_quiet() { git -c user.email=t@example -c user.name=t "$@" >/dev/null 2>&1; }

# A digest of the whole tree — the evidence for "wrote nothing". `.git` and
# `__pycache__` are excluded: Git rewrites index metadata on read commands, and
# CPython writes bytecode whenever a module is imported. Neither is the
# initializer's doing.
tree_digest() {
  find "$1" \( -name .git -o -name __pycache__ \) -prune -o -print 2>/dev/null \
    | sort | while IFS= read -r item; do
        if [[ -f "$item" ]]; then printf '%s %s\n' "$(md5sum <"$item" | cut -d' ' -f1)" "$item"
        else printf 'dir %s\n' "$item"; fi
      done | md5sum
}

make_father() {
  local father="$WORK/$1"
  mkdir -p "$father"
  cp -r "$TOOLKIT" "$father/toolkit"
  rm -rf "$father/toolkit/.git" "$father/toolkit/graft" "$father/toolkit/workspace.json" \
         "$father/toolkit/DESIGN.md" "$father/toolkit/UI-DESIGN.md"
  local repo
  for repo in example-api example-web; do
    mkdir -p "$father/$repo"
    git_quiet -C "$father/$repo" init -b main
    git_quiet -C "$father/$repo" commit --allow-empty -m init
  done
  mkdir -p "$father/just-a-folder"
  printf '%s' "$father"
}

echo ""
echo "=== the default plan writes nothing"
father=$(make_father plan)
before=$(tree_digest "$father")
out=$("$father/toolkit/init.sh" --product example --name "Example Product" 2>&1); status=$?
after=$(tree_digest "$father")
check "planning exits 0" 0 "$status"
check "planning changed no file and no directory" "$before" "$after"
contains "planning says so" "$out" "PLAN ONLY"
contains "it proposes an alias per repository" "$out" "example-api"
contains "it lists what it would create" "$out" "WOULD CREATE"
contains "a non-Git sibling is ignored" "$out" "just-a-folder"
[[ -f "$father/toolkit/workspace.json" ]] && no "no configuration was written" || ok "no configuration was written"
[[ -d "$father/workspace-data" ]] && no "no data directory was created" || ok "no data directory was created"

echo ""
echo "=== --apply creates the configuration and the skeleton"
out=$("$father/toolkit/init.sh" --product example --name "Example Product" --apply 2>&1); status=$?
check "apply exits 0" 0 "$status"
[[ -f "$father/toolkit/workspace.json" ]] && ok "workspace.json exists" || no "workspace.json exists"
[[ -d "$father/workspace-data/LLM/prompts/example-api/done" ]] \
  && ok "the queue skeleton exists" || no "the queue skeleton exists"
[[ -d "$father/workspace-data/LLM/handoffs/example-web" ]] \
  && ok "the handoff skeleton exists" || no "the handoff skeleton exists"
contains "child seeds are reported prominently" "$out" "FILES WERE PLACED INSIDE CHILD REPOSITORIES"

echo ""
echo "=== the written configuration drives the runners"
out=$("$father/toolkit/run-sequence.sh" --repos 2>&1); status=$?
check "run-sequence.sh reads it" 0 "$status"
contains "it lists the discovered repositories" "$out" "example-api"
out=$("$father/toolkit/run-sequence.sh" --extract-sequence 2>&1); status=$?
check "the planner reads it" 0 "$status"
contains "the planner sees the repositories" "$out" "repositories inspected:  2"

echo ""
echo "=== a second identical --apply is a no-op"
before=$(tree_digest "$father")
out=$("$father/toolkit/init.sh" --product example --name "Example Product" --apply 2>&1)
after=$(tree_digest "$father")
check "the second apply changed nothing" "$before" "$after"
contains "it says it created nothing" "$out" "CREATED (0)"

echo ""
echo "=== existing documents are never overwritten"
father=$(make_father preserve)
for name in AGENTS.md DESIGN.md UI-DESIGN.md README.md WORKFLOW.md; do
  printf 'HAND WRITTEN %s\n' "$name" >"$father/toolkit/$name"
done
for name in AGENTS.md DESIGN.md; do
  printf 'HAND WRITTEN child %s\n' "$name" >"$father/example-api/$name"
done
# Captured BEFORE the apply, so the comparison below is worth making.
head_before=$(git -C "$father/example-web" rev-parse HEAD)
"$father/toolkit/init.sh" --product example --name "Example" --apply >/dev/null 2>&1
if [[ "$(cat "$father/toolkit/DESIGN.md")" == "HAND WRITTEN DESIGN.md" ]]; then
  ok "an existing DESIGN.md is byte-identical"
else
  no "an existing DESIGN.md is byte-identical"
fi
if [[ "$(cat "$father/toolkit/UI-DESIGN.md")" == "HAND WRITTEN UI-DESIGN.md" ]]; then
  ok "an existing UI-DESIGN.md is byte-identical"
else
  no "an existing UI-DESIGN.md is byte-identical"
fi
if [[ "$(cat "$father/example-api/AGENTS.md")" == "HAND WRITTEN child AGENTS.md" ]]; then
  ok "an existing child AGENTS.md is byte-identical"
else
  no "an existing child AGENTS.md is byte-identical"
fi
# example-web had none, so it gets seeds; example-api had its own and keeps them.
[[ -f "$father/example-web/AGENTS.md" ]] && ok "a child without one gets a pointer" \
  || no "a child without one gets a pointer"

echo ""
echo "=== child repositories are never committed"
head_after=$(git -C "$father/example-web" rev-parse HEAD)
check "the child repository's HEAD did not move" "$head_before" "$head_after"
status_out=$(git -C "$father/example-web" status --porcelain)
contains "the seed is left uncommitted for review" "$status_out" "AGENTS.md"
remotes=$(git -C "$father/example-web" remote -v)
check "no remote was added" "" "$remotes"

echo ""
echo "=== an existing configuration is authoritative"
father=$(make_father authoritative)
cat >"$father/toolkit/workspace.json" <<CONFIG
{
  "schema": "auto-pigeon-toolkit-workspace/1.0",
  "product": { "id": "chosen", "name": "Chosen By Operator" },
  "data_root": "../chosen-data",
  "repositories": [ { "alias": "MYNAME", "path": "../example-api" } ]
}
CONFIG
before=$(md5sum <"$father/toolkit/workspace.json")
out=$("$father/toolkit/init.sh" --product different --name "Different" --apply 2>&1)
after=$(md5sum <"$father/toolkit/workspace.json")
check "the configuration was not regenerated" "$before" "$after"
contains "the operator's product is used" "$out" "chosen"
contains "the operator's alias is used" "$out" "MYNAME"
contains "an unconfigured sibling is reported" "$out" "DISCREPANCIES"
[[ -d "$father/chosen-data/LLM/prompts/MYNAME" ]] \
  && ok "the skeleton follows the operator's alias" \
  || no "the skeleton follows the operator's alias"

echo ""
echo "=== a custom data directory"
father=$(make_father custom-data)
"$father/toolkit/init.sh" --product example --data-dir ../operational-data --apply >/dev/null 2>&1
[[ -d "$father/operational-data/LLM/prompts" ]] \
  && ok "--data-dir is honoured" || no "--data-dir is honoured"
[[ -d "$father/workspace-data" ]] && no "the default data directory was not created" \
  || ok "the default data directory was not created"

echo ""
echo "=== an absolute data directory stays absolute"
father=$(make_father absolute-data)
"$father/toolkit/init.sh" --product example --data-dir "$WORK/elsewhere" --apply >/dev/null 2>&1
stored=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data_root"])' \
           "$father/toolkit/workspace.json")
check "the operator's absolute path is stored as written" "$WORK/elsewhere" "$stored"

echo ""
echo "=== a first run without --product refuses rather than guessing"
father=$(make_father no-product)
out=$("$father/toolkit/init.sh" 2>&1); status=$?
check "it exits 2" 2 "$status"
contains "it says what is missing" "$out" "--product"

echo ""
echo "---------------------------------------------------------------"
printf 'passed: %d   failed: %d\n' "$passed" "$failed"
[[ $failed -eq 0 ]] || exit 1
