#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
DB="$APP_DIR/data/casefiles.db"

if [[ ! -f "$DB" ]]; then
  echo "No database found at $DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
sqlite3 "$DB" ".backup '$BACKUP_DIR/tam-console-$stamp.db'"
echo "$BACKUP_DIR/tam-console-$stamp.db"
