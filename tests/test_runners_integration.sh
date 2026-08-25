#!/usr/bin/env bash
set -uo pipefail

# Shell integration for the two runners, end to end, against a DISPOSABLE
# workspace and a STUB agent.
#
# WHY A STUB AND NOT A REAL AGENT. What is under test here is the runner: does it
# resolve the configured repository, dispatch the right prompt, notice that the
# prompt finished, refuse a dirty worktree, honour --allow-dirty, record timing.
# None of that needs a model, and paying for one to prove that a process was
# started would prove nothing extra.
#
# The stub is pointed at through `agents.<name>.command` in workspace.json, which
# is the same mechanism an operator uses to point at a wrapper — so this exercises
# the real configuration path rather than a test-only hook.

TOOLKIT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

passed=0
failed=0

ok() { printf '  ok    %s\n' "$1"; passed=$((passed + 1)); }
no() { printf '  FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; failed=$((failed + 1)); }

check() {
  # check <name> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else no "$1" "expected [$2], got [$3]"; fi
}

contains() {
  # contains <name> <haystack> <needle>
  if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "missing [$3]"; fi
}

not_contains() {
  if [[ "$2" != *"$3"* ]]; then ok "$1"; else no "$1" "unexpectedly present [$3]"; fi
}

git_quiet() { git -c user.email=t@example -c user.name=t "$@" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# A fresh workspace per scenario: father/toolkit + father/<repos> + data root.
# ---------------------------------------------------------------------------
make_workspace() {
  # $1 = scenario name. Echoes the father directory.
  local father="$WORK/$1"
  mkdir -p "$father"
  cp -r "$TOOLKIT" "$father/toolkit"
  rm -rf "$father/toolkit/.git" "$father/toolkit/graft" "$father/toolkit/workspace.json"
  local repo
  for repo in example-api "example web"; do
    mkdir -p "$father/$repo"
    git_quiet -C "$father/$repo" init -b main
    git_quiet -C "$father/$repo" commit --allow-empty -m init
  done
  # The stub, written per workspace so it can name its own toolkit.
  cat >"$father/stub-agent" <<'STUB'
#!/usr/bin/env bash
# Everything a finished prompt has to do, and nothing else: write the handoff,
# commit by pathspec, move the prompt into done/.
set -euo pipefail
message="${*: -1}"
prompt_rel=$(grep -o 'LLM/prompts/[^ ]*\.md' <<<"$message" | head -1) || true
[[ -n "$prompt_rel" ]] || { echo "stub-agent: no prompt named in the message" >&2; exit 3; }
alias_name=$(basename "$(dirname "$prompt_rel")")
prompt_file=$(basename "$prompt_rel")
repo=$(python3 "$STUB_TOOLKIT/scripts/workspace_config.py" --path "$alias_name")
printf 'work for %s\n' "$prompt_file" >>"$repo/AGENT-OUTPUT.txt"
git -C "$repo" -c user.email=stub@example -c user.name=stub \
  commit -q -m "stub: $prompt_file" -- AGENT-OUTPUT.txt 2>/dev/null \
  || { git -C "$repo" add -- AGENT-OUTPUT.txt
       git -C "$repo" -c user.email=stub@example -c user.name=stub \
         commit -q -m "stub: $prompt_file" -- AGENT-OUTPUT.txt; }
python3 "$STUB_TOOLKIT/scripts/agent_task.py" checkpoint --repo-root "$repo" \
  --prompt "$prompt_file" --status complete >/dev/null
mkdir -p "$WORKSPACE_DATA_ROOT/LLM/prompts/$alias_name/done"
mv "$WORKSPACE_DATA_ROOT/LLM/prompts/$alias_name/$prompt_file" \
   "$WORKSPACE_DATA_ROOT/LLM/prompts/$alias_name/done/$prompt_file"
echo "stub-agent: completed $prompt_file"
STUB
  chmod +x "$father/stub-agent"

  # A configuration written by hand rather than by init.sh: these scenarios are
  # about the RUNNERS, and one of them deliberately uses a path with a space.
  cat >"$father/toolkit/workspace.json" <<CONFIG
{
  "schema": "auto-pigeon-toolkit-workspace/1.0",
  "product": { "id": "example", "name": "Example Product" },
  "data_root": "../workspace-data",
  "repositories": [
    { "alias": "api", "path": "../example-api" },
    { "alias": "web", "path": "../example web" }
  ],
  "agents": {
    "claude": { "enabled": true, "command": "$father/stub-agent" },
    "codex":  { "enabled": true, "command": "$father/stub-agent" }
  }
}
CONFIG
  local alias
  for alias in api web; do
    mkdir -p "$father/workspace-data/LLM/prompts/$alias/done" \
             "$father/workspace-data/LLM/prompts/$alias/blocked" \
             "$father/workspace-data/LLM/handoffs/$alias"
  done
  printf '%s' "$father"
}

add_prompt() {
  # add_prompt <father> <alias> <filename> [extra frontmatter]
  local father=$1 alias=$2 name=$3 extra=${4:-}
  cat >"$father/workspace-data/LLM/prompts/$alias/$name" <<EOF
---
task_id: ${name%.md}
repo: $alias
mutation_targets:
  - $alias
$extra
---

# ${name%.md}

Do the thing.
EOF
}

echo ""
echo "=== run-agent.sh: a configured repository, resolved from workspace.json"
father=$(make_workspace agent-smoke)
export STUB_TOOLKIT="$father/toolkit"
add_prompt "$father" api 20260801_01_First.md
out=$("$father/toolkit/run-agent.sh" api 2>&1); status=$?
check "run-agent.sh exits 0" 0 "$status"
contains "the stub was dispatched the resolved prompt" "$out" "completed 20260801_01_First.md"
[[ -f "$father/workspace-data/LLM/prompts/api/done/20260801_01_First.md" ]] \
  && ok "the prompt was moved into done/" || no "the prompt was moved into done/"
[[ -f "$father/workspace-data/LLM/handoffs/api/20260801_01_First.md" ]] \
  && ok "a handoff was written under the configured data root" \
  || no "a handoff was written under the configured data root"

echo ""
echo "=== run-agent.sh: a repository path containing spaces"
add_prompt "$father" web 20260801_02_Spaced.md
out=$("$father/toolkit/run-agent.sh" web 2>&1); status=$?
check "a path with a space runs" 0 "$status"
[[ -f "$father/workspace-data/LLM/prompts/web/done/20260801_02_Spaced.md" ]] \
  && ok "the spaced repository's prompt completed" \
  || no "the spaced repository's prompt completed"

echo ""
echo "=== run-agent.sh: alias handling"
add_prompt "$father" api 20260801_03_Third.md
out=$("$father/toolkit/run-agent.sh" API --dry-run 2>&1)
contains "an alias resolves case-insensitively" "$out" '"repo": "api"'
out=$("$father/toolkit/run-agent.sh" nosuchrepo --dry-run 2>&1); status=$?
check "an unknown alias exits 2" 2 "$status"
contains "an unknown alias names the configured list" "$out" "api"
contains "and every configured alias, not just the first" "$out" "web"
# The strong form of "there is no second alias table": the runner knows exactly as
# many repositories as the configuration names. Asserted by counting rather than
# by naming a retired alias, which the topology guard would (rightly) flag.
repo_lines=$("$father/toolkit/run-sequence.sh" --repos | wc -l)
check "the runner knows exactly the configured repositories" 2 "$repo_lines"

echo ""
echo "=== run-sequence.sh: an explicit queue of several prompts"
father=$(make_workspace sequence-queue)
export STUB_TOOLKIT="$father/toolkit"
add_prompt "$father" api 20260801_01_First.md
add_prompt "$father" api 20260801_02_Second.md
out=$("$father/toolkit/run-sequence.sh" --queue api \
        20260801_01_First.md 20260801_02_Second.md 2>&1); status=$?
check "an explicit list exits 0" 0 "$status"
contains "both prompts completed" "$out" "prompts completed:   2"
[[ -f "$father/workspace-data/LLM/prompts/api/done/20260801_02_Second.md" ]] \
  && ok "the second prompt reached done/" || no "the second prompt reached done/"

echo ""
echo "=== run-sequence.sh: dependency-aware selection across configured aliases"
father=$(make_workspace sequence-depends)
export STUB_TOOLKIT="$father/toolkit"
add_prompt "$father" api 20260801_01_First.md
add_prompt "$father" web 20260801_02_Second.md \
  "requires:
  - repo: api
    prompt: 20260801_01_First.md"
plan=$("$father/toolkit/run-sequence.sh" --extract-sequence --format shell 2>&1)
first_line=$(head -1 <<<"$plan")
contains "the plan starts with the prerequisite's repository" "$first_line" "--queue api"
contains "the plan emits the configured alias" "$plan" "--queue web"
out=$("$father/toolkit/run-sequence.sh" 2>&1); status=$?
check "a drain of every configured queue exits 0" 0 "$status"
contains "both repositories were drained" "$out" "prompts completed:   2"

echo ""
echo "=== run-sequence.sh: the dirty-worktree default is refusal"
father=$(make_workspace dirty-default)
export STUB_TOOLKIT="$father/toolkit"
add_prompt "$father" api 20260801_01_First.md
echo "someone else's work" >"$father/example-api/UNCOMMITTED.txt"
out=$("$father/toolkit/run-sequence.sh" --queue api 2>&1); status=$?
check "a dirty worktree refuses by default" 1 "$status"
contains "the refusal names the reason" "$out" "worktree was dirty"
[[ -f "$father/workspace-data/LLM/prompts/api/20260801_01_First.md" ]] \
  && ok "the refused prompt stayed in the queue" || no "the refused prompt stayed in the queue"
[[ -f "$father/example-api/UNCOMMITTED.txt" ]] \
  && ok "the pre-existing work was left alone" || no "the pre-existing work was left alone"

echo ""
echo "=== run-sequence.sh: --allow-dirty records a baseline and still runs"
out=$("$father/toolkit/run-sequence.sh" --allow-dirty --queue api 2>&1); status=$?
contains "--allow-dirty announces what it is starting over" "$out" "STARTING OVER PRE-EXISTING UNCOMMITTED WORK"
baselines=$(find "$father/workspace-data/.run-sequence" -name '*.dirty-baseline.json' 2>/dev/null | wc -l)
[[ "$baselines" -ge 1 ]] \
  && ok "a baseline was written under the configured data root" \
  || no "a baseline was written under the configured data root" "found none"
contains "preservation is reported either way" "$out" "dirty preservation:"
[[ -f "$father/example-api/UNCOMMITTED.txt" ]] \
  && ok "the pre-existing work survived the run" || no "the pre-existing work survived the run"
"$father/toolkit/run-sequence.sh" --accept-dirty --queue api --dry-run >/dev/null 2>&1; status2=$?
check "--accept-dirty is accepted as an alias" 0 "$status2"

echo ""
echo "=== read-only modes write nothing"
father=$(make_workspace read-only)
export STUB_TOOLKIT="$father/toolkit"
add_prompt "$father" api 20260801_01_First.md
before=$(find "$father/workspace-data" -type f -o -type d | sort | md5sum)
"$father/toolkit/run-sequence.sh" --extract-sequence >/dev/null 2>&1
"$father/toolkit/run-sequence.sh" --history >/dev/null 2>&1
"$father/toolkit/run-sequence.sh" --dry-run >/dev/null 2>&1
"$father/toolkit/run-agent.sh" api --dry-run >/dev/null 2>&1
after=$(find "$father/workspace-data" -type f -o -type d | sort | md5sum)
check "extract-sequence/history/dry-run changed nothing" "$before" "$after"

echo ""
echo "=== history reports under the configured data root"
father=$(make_workspace history)
export STUB_TOOLKIT="$father/toolkit"
add_prompt "$father" api 20260801_01_First.md
"$father/toolkit/run-sequence.sh" --queue api >/dev/null 2>&1
out=$("$father/toolkit/run-sequence.sh" --history 2>&1)
contains "the completed prompt appears in history" "$out" "20260801_01_First.md"
contains "its measurement is the runner's" "$out" "runner"
contains "the alias is not truncated" "$out" "api "
json=$("$father/toolkit/run-sequence.sh" --history --format json 2>&1)
contains "history has a stable JSON form" "$json" '"measurement"'

echo ""
echo "=== both configured agents are dispatchable"
father=$(make_workspace agents)
export STUB_TOOLKIT="$father/toolkit"
add_prompt "$father" api 20260801_01_First.md
add_prompt "$father" api 20260801_02_Second.md
out=$("$father/toolkit/run-agent.sh" --agent claude api 2>&1); status=$?
check "--agent claude runs" 0 "$status"
out=$("$father/toolkit/run-agent.sh" --agent codex api 2>&1); status=$?
check "--agent codex runs" 0 "$status"
python3 - "$father/toolkit/workspace.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["agents"]["codex"]["enabled"] = False
json.dump(data, open(path, "w"), indent=2)
PY
add_prompt "$father" api 20260801_03_Third.md
out=$("$father/toolkit/run-agent.sh" --agent codex api 2>&1); status=$?
check "a disabled agent refuses" 2 "$status"
contains "the refusal says it is disabled" "$out" "disabled"

echo ""
echo "=== a missing configuration instructs the operator"
father=$(make_workspace no-config)
rm -f "$father/toolkit/workspace.json"
out=$("$father/toolkit/run-agent.sh" api 2>&1); status=$?
check "no configuration exits 2" 2 "$status"
contains "the error names init.sh" "$out" "./init.sh"
out=$("$father/toolkit/run-sequence.sh" --queue api 2>&1); status=$?
check "run-sequence.sh also exits 2" 2 "$status"
contains "run-sequence.sh also names init.sh" "$out" "./init.sh"

echo ""
echo "=== a malformed configuration fails visibly"
father=$(make_workspace bad-config)
echo '{ "product": { "id": "x" }, ' >"$father/toolkit/workspace.json"
out=$("$father/toolkit/run-sequence.sh" --queue api 2>&1); status=$?
check "malformed JSON exits 2" 2 "$status"
contains "the error says it is not valid JSON" "$out" "not valid JSON"

echo ""
echo "=== --help and --repos are side-effect free"
father=$(make_workspace help)
before=$(find "$father" -type f | sort | md5sum)
"$father/toolkit/run-agent.sh" --help >/dev/null
"$father/toolkit/run-sequence.sh" --help >/dev/null
"$father/toolkit/init.sh" --help >/dev/null
out=$("$father/toolkit/run-sequence.sh" --repos 2>&1)
after=$(find "$father" -type f | sort | md5sum)
check "help and --repos wrote nothing" "$before" "$after"
contains "--repos lists a configured alias" "$out" "api"

echo ""
echo "---------------------------------------------------------------"
printf 'passed: %d   failed: %d\n' "$passed" "$failed"
[[ $failed -eq 0 ]] || exit 1
