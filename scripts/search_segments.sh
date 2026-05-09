#!/usr/bin/env bash
set -euo pipefail

: <<'EOF'
usage:

# Basic search across all clips
./scripts/search_segments.sh "Feixiao Ultimate animation"

# Restrict to clips whose video id contains "idle"
./scripts/search_segments.sh "idle animation" --video-contains idle --max-results 3

Optional flags:
  --profile <name>            # override EVENT_METADATA_PROFILE/.env default (e.g., hsr)
  --max-results <n>           # number of event metadata spans to return (default: 50)
  --video-contains <substr>   # pre-filter candidate videos by substring match
  --samples-per-video <n>     # accepted for backward compatibility; ignored
EOF

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <search phrase> [--profile NAME] [--max-results N] [--video-contains SUBSTR] [--samples-per-video N]" >&2
    exit 1
fi

QUERY="$1"
shift || true
PROFILE=""
MAX_RESULTS=50
VIDEO_CONTAINS=""
SAMPLES_PER_VIDEO=200

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            PROFILE="$2"
            shift 2
            ;;
        --profile=*)
            PROFILE="${1#*=}"
            shift 1
            ;;
        --max-results)
            MAX_RESULTS="$2"
            shift 2
            ;;
        --max-results=*)
            MAX_RESULTS="${1#*=}"
            shift 1
            ;;
        --video-contains)
            VIDEO_CONTAINS="$2"
            shift 2
            ;;
        --video-contains=*)
            VIDEO_CONTAINS="${1#*=}"
            shift 1
            ;;
        --samples-per-video)
            SAMPLES_PER_VIDEO="$2"
            shift 2
            ;;
        --samples-per-video=*)
            SAMPLES_PER_VIDEO="${1#*=}"
            shift 1
            ;;
        --)
            shift
            break
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

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

CMD=("${PYTHON_BIN}" -m gateway.vision.event_metadata.cli search "${QUERY}" --limit "${MAX_RESULTS}")
if [[ -n "${PROFILE}" ]]; then
    CMD+=(--profile "${PROFILE}")
fi
if [[ -n "${VIDEO_CONTAINS}" ]]; then
    CMD+=(--video-id-contains "${VIDEO_CONTAINS}")
fi

PYTHONIOENCODING=utf-8 "${CMD[@]}"
