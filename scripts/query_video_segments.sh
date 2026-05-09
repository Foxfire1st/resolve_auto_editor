#!/usr/bin/env bash
set -euo pipefail

: <<'EOF'
some examples:

Lists all videos and show their names and segment numbers
PYTHONIOENCODING=utf-8 sh ./query_video_segments.sh --contains "%" --limit 500 | awk -F' duration=' '/^video=/{val=$1; sub(/^video=/,"",val); print val}'

-- Beware of potential Encoding Issues with foreign char sets ---
because Python tries to emit UTF-8 characters (likely from captions/tags) but, when stdout is piped, Windows falls back to the console's CP1252 encoding. 
Anything outside CP1252 (Chinese, emoji, etc.) raises an UnicodeEncodeError.
Some clips can have non-ASCII tags/details from Gemini metadata. This needs to be kept in mind
in our pipeline for potential issues.

EOF

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/.."
cd "${REPO_ROOT}"

if [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON_BIN=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
else
    echo "error: repo virtualenv not found; create .venv before running this script" >&2
    exit 1
fi

export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

"${PYTHON_BIN}" -m gateway.vision.event_metadata.cli describe "$@"
