#!/usr/bin/env bash
# Launches Zotero PDF Export on macOS / Linux. Prefers `python3`, falls back to `python`.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$DIR/zotero_export.py" "$@"
else
    exec python "$DIR/zotero_export.py" "$@"
fi
