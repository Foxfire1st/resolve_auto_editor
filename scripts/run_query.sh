#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/.."
cd "${REPO_ROOT}"

if [[ -x ".venv/Scripts/python.exe" ]]; then
    PYTHON_BIN=".venv/Scripts/python.exe"
elif [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
else
    echo "error: repo virtualenv not found; create .venv before running this script" >&2
    exit 1
fi

export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

PYTHONIOENCODING=utf-8 "${PYTHON_BIN}" - <<'PY'
from gateway.vision.event_metadata.search import search_events

queries = [
    "Feixiao ultimate description",
    "Feixiao traces ultimate",
    "Feixiao trace ultimate",
]
for query in queries:
    print('---', query)
    results = search_events(query, profile_name='hsr', limit=5)
    for idx, res in enumerate(results, 1):
        print(idx, round(res.score, 3), res.video_id, f"{res.start_s:.1f}-{res.end_s:.1f}", res.summary)
    print()
PY
