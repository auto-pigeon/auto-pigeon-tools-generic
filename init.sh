#!/usr/bin/env bash
set -euo pipefail

# init.sh — configure this toolkit checkout for one product.
#
# A THIN WRAPPER, DELIBERATELY. Every decision — which siblings are repositories,
# which aliases they get, what a seed document says, what gets written — lives in
# scripts/workspace_init.py, where it is testable without a shell. This file
# parses flags, refuses the ones that make no sense together, and hands over.
#
# THE DEFAULT WRITES NOTHING. Without --apply this prints the plan and exits;
# that is what makes it safe to run against a workspace you have not decided
# about yet.

print_usage() {
  cat <<'HELPTEXT'
init.sh — configure this toolkit checkout for one product.

USAGE
  ./init.sh [--product ID] [--name NAME] [--data-dir PATH]           plan only
  ./init.sh [--product ID] [--name NAME] [--data-dir PATH] --apply   write it
  ./init.sh --help

WHAT THE DEFAULT DOES
  Resolves this toolkit's directory and its father, looks at the IMMEDIATE
  siblings only, identifies which of them are Git repository roots, excludes
  itself and the data directory, proposes an alias per repository, inventories
  which seed documents already exist, and prints exactly what --apply would
  create.

  It writes nothing. No directory is created, no repository is initialized, no
  agent is started, and nothing under the data root is read beyond checking
  which directories exist.

WHAT --apply DOES
  Creates, and only when missing:
    * workspace.json
    * the data directory skeleton
    * this repository's seed documents
    * AGENTS.md and DESIGN.md inside each configured child repository

  It never overwrites or merges an existing file, never commits anything in a
  child repository, never pushes, and never modifies a Git remote. Running it a
  second time with the same arguments changes nothing.

  Files created inside child repositories make those repositories DIRTY. They
  are reported prominently; committing them is your decision.

OPTIONS
  --product ID     the product id. Required the first time, when there is no
                   workspace.json yet.
  --name NAME      human-readable product name (default: the id)
  --data-dir PATH  where operational data lives. Relative paths resolve from
                   this repository. Default: ../workspace-data
  --config PATH    write/read a workspace.json somewhere other than this
                   repository's root. Mostly for tests.
  --apply          make the changes
  --json           machine-readable plan
  -h, --help       this text, on stdout, exit 0

IF workspace.json ALREADY EXISTS
  It is AUTHORITATIVE. Its product, its data root and its repository list are
  used as written; discovery still runs, but only to report differences. Aliases
  you chose are never silently replaced, and the file is never regenerated.

EXAMPLES
  ./init.sh --product example --name "Example Product"
  ./init.sh --product example --name "Example Product" --apply
  ./init.sh --data-dir /srv/example-data --product example --apply
  ./init.sh                       re-plan against the existing workspace.json
HELPTEXT
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  print_usage
  exit 0
fi

usage() {
  print_usage >&2
  exit 2
}

product=""
name=""
data_dir=""
config=""
apply=false
json=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) print_usage; exit 0 ;;
    --product) [[ $# -ge 2 ]] || usage; product=$2; shift 2 ;;
    --product=*) product="${1#--product=}"; shift ;;
    --name) [[ $# -ge 2 ]] || usage; name=$2; shift 2 ;;
    --name=*) name="${1#--name=}"; shift ;;
    --data-dir) [[ $# -ge 2 ]] || usage; data_dir=$2; shift 2 ;;
    --data-dir=*) data_dir="${1#--data-dir=}"; shift ;;
    --config) [[ $# -ge 2 ]] || usage; config=$2; shift 2 ;;
    --config=*) config="${1#--config=}"; shift ;;
    --apply) apply=true; shift ;;
    --json) json=true; shift ;;
    -*) echo "unknown flag: $1" >&2; echo "" >&2; usage ;;
    *) echo "init.sh takes no positional arguments: $1" >&2; echo "" >&2; usage ;;
  esac
done

# `readlink -f` first: this may be invoked through a symlink, and `dirname` on a
# symlink gives the LINK's directory rather than this repository's.
toolkit_root=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found on PATH" >&2; exit 2; }

args=("--toolkit-root" "$toolkit_root")
[[ -n "$product" ]] && args+=("--product" "$product")
[[ -n "$name" ]] && args+=("--name" "$name")
[[ -n "$data_dir" ]] && args+=("--data-dir" "$data_dir")
[[ -n "$config" ]] && args+=("--config" "$config")
[[ "$apply" == true ]] && args+=("--apply")
[[ "$json" == true ]] && args+=("--json")

exec python3 "$toolkit_root/scripts/workspace_init.py" "${args[@]}"
