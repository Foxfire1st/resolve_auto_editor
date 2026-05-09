#!/usr/bin/env bash
set -euo pipefail

: <<'EOF'
usage:

Running this script will ingest all clips for a given video project into event_metadata.db
so the planner can look up editor-usable visual event spans.
Video Folder paths have to be present in the project.settings.json. 
And each clip folder needs to have a meta folder within. 
Before ingesting event metadata, run each clip through gemini_ingest first so each meta file contains
structured events with start/end spans.

./scripts/run_ingest.sh "samples/feixiao test edit"
EOF

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <project_dir> [batch-ingest-args...]" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/.."
PROJECT_DIR="$(cd "$1" && pwd)"
shift || true
EXTRA_ARGS=("$@")
PROFILE_ARGS=("--profile" "hsr")
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    for arg in "${EXTRA_ARGS[@]}"; do
        if [[ "${arg}" == --profile || "${arg}" == --profile=* ]]; then
            PROFILE_ARGS=()
            break
        fi
    done
fi

SETTINGS_PATH="${PROJECT_DIR}/project.settings.json"
if [[ ! -f "${SETTINGS_PATH}" ]]; then
    echo "project.settings.json not found at ${SETTINGS_PATH}" >&2
    exit 1
fi

if [[ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="${REPO_ROOT}/.venv/Scripts/python.exe"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
else
    echo "error: repo virtualenv not found; create .venv before running this script" >&2
    exit 1
fi

export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

readarray -t INGEST_GROUPS < <(
    "${PYTHON_BIN}" - <<'PY' "${SETTINGS_PATH}" "${PROJECT_DIR}"
import json
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])
project_dir = Path(sys.argv[2])
try:
    data = json.loads(settings_path.read_text(encoding="utf-8"))
except Exception as exc:  # pragma: no cover - defensive parsing
    print(f"Failed to read {settings_path}: {exc}", file=sys.stderr)
    sys.exit(1)

video_section = data.get("media_types", {}).get("video", {})
if not video_section:
    print("No video media_types defined in project settings", file=sys.stderr)
    sys.exit(1)

groups: dict[Path, set[str]] = {}
missing: list[str] = []
for entries in video_section.values():
    if not isinstance(entries, list):
        continue
    for rel in entries:
        candidate = (project_dir / rel).resolve()
        if not candidate.exists():
            missing.append(str(candidate))
            continue
        groups.setdefault(candidate.parent, set()).add(candidate.name)

if missing:
    print("Missing footage folders:\n" + "\n".join(missing), file=sys.stderr)
    sys.exit(1)

if not groups:
    print("No footage folders discovered in project settings", file=sys.stderr)
    sys.exit(1)

for parent in sorted(groups.keys()):
    folders = " ".join(sorted(groups[parent]))
    print(f"{parent}|{folders}")
PY
)

if [[ ${#INGEST_GROUPS[@]} -eq 0 ]]; then
    echo "No footage folders resolved from project settings" >&2
    exit 1
fi

for entry in "${INGEST_GROUPS[@]}"; do
    ROOT_DIR="${entry%%|*}"
    FOLDERS_STRING="${entry#*|}"
    ROOT_DIR="${ROOT_DIR//$'\r'/}"
    FOLDERS_STRING="${FOLDERS_STRING//$'\r'/}"
    if [[ -z "${FOLDERS_STRING}" ]]; then
        continue
    fi
    read -r -a FOLDERS <<< "${FOLDERS_STRING}"
    if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
        "${PYTHON_BIN}" -m gateway.vision.event_metadata.cli batch-ingest \
            --footage-root "${ROOT_DIR}" \
            --folders "${FOLDERS[@]}" \
            "${PROFILE_ARGS[@]}" \
            "${EXTRA_ARGS[@]}"
    else
        "${PYTHON_BIN}" -m gateway.vision.event_metadata.cli batch-ingest \
            --footage-root "${ROOT_DIR}" \
            --folders "${FOLDERS[@]}" \
            "${PROFILE_ARGS[@]}"
    fi
done
