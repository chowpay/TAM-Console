#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

blocked_paths="$(
  git ls-files |
    grep -E '(^data/|^notes/|^backups/|^config/atlassian_config\.py$|\.db$|\.sqlite$|\.sqlite3$)' || true
)"

if [[ -n "$blocked_paths" ]]; then
  echo "Public-safety check failed: private runtime paths are tracked." >&2
  echo "$blocked_paths" >&2
  exit 1
fi

denylist="config/public_safety_denylist.local"
if [[ -f "$denylist" ]]; then
  patterns="$(mktemp)"
  trap 'rm -f "$patterns"' EXIT
  grep -vE '^[[:space:]]*(#|$)' "$denylist" > "$patterns" || true
  if [[ -s "$patterns" ]]; then
    matches="$(git grep -n -i -f "$patterns" HEAD -- . || true)"
    if [[ -n "$matches" ]]; then
      echo "Public-safety check failed: tracked files match local denylist terms." >&2
      echo "$matches" >&2
      exit 1
    fi
  fi
fi

echo "Public-safety check passed."
