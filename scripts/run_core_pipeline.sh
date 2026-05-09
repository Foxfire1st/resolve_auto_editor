#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -z "${PYTHON_BIN:-}" ]]; then
    if [[ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
        PYTHON_BIN="${REPO_ROOT}/.venv/Scripts/python.exe"
    elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
    else
        echo "error: repo virtualenv not found; create .venv before running this script" >&2
        exit 1
    fi
fi
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

PROJECT_ROOT="${PROJECT_ROOT:-${REPO_ROOT}/samples/feixiao test edit}"
PROJECT_NAME="${PROJECT_NAME:-Feixiao Auto-Edit}"
TIMELINE_NAME="${TIMELINE_NAME:-Core Edit V1}"
FCPXML_PATH="${FCPXML_PATH:-${PROJECT_ROOT}/outputs/core_edit_focus.fcpxml}"
NOTES_PATH="${NOTES_PATH:-${PROJECT_ROOT}/outputs/director_notes_focus.json}"
RELINK_REPORT="${RELINK_REPORT:-${PROJECT_ROOT}/outputs/relink_report.json}"
SEGMENT_INDEX="${SEGMENT_INDEX:-${PROJECT_ROOT}/alignment_segments.json}"
BEAT_MAP="${BEAT_MAP:-${PROJECT_ROOT}/script_breakdown.json}"
VO_AUDIO="${VO_AUDIO:-${PROJECT_ROOT}/audio/Feixiao Guide Voice Over.wav}"
SCRIPT_PATH="${SCRIPT_PATH:-${PROJECT_ROOT}/script.md}"
PROJECT_SETTINGS="${PROJECT_SETTINGS:-${PROJECT_ROOT}/project.settings.json}"
FOOTAGE_ROOT="${FOOTAGE_ROOT:-}"
MANIFEST_PATH="${MANIFEST_PATH:-${PROJECT_ROOT}/outputs/footage_manifest.ndjson}"
MANIFEST_HANDLE="${MANIFEST_HANDLE:-${MANIFEST_PATH%.ndjson}.handle.json}"
ATTACHMENT_STATE="${ATTACHMENT_STATE:-${PROJECT_ROOT}/outputs/gemini_attachments.json}"
BUILD_MANIFEST="${BUILD_MANIFEST:-1}"
VERIFY_ATTACHMENTS="${VERIFY_ATTACHMENTS:-0}"

if [ -n "${FOOTAGE_ROOT}" ]; then
  FOOTAGE_ARGS=(--footage-root "${FOOTAGE_ROOT}")
else
  FOOTAGE_ARGS=()
fi

mkdir -p "${PROJECT_ROOT}/outputs"

if [ "${BUILD_MANIFEST}" != "0" ]; then
  echo "[pipeline] Building footage manifest artifact..."
  "${PYTHON_BIN}" -m src.orchestrator.cli.build_footage_manifest \
    --project-settings "${PROJECT_SETTINGS}" \
    --output "${MANIFEST_PATH}" \
    "${FOOTAGE_ARGS[@]}"
fi

if [ -z "${SKIP_BEAT_MAP:-}" ]; then
  echo "[pipeline] Generating beat map grounded in footage summaries..."
  "${PYTHON_BIN}" -m src.orchestrator.cli.generate_beat_map \
    --segments "${SEGMENT_INDEX}" \
    --script "${SCRIPT_PATH}" \
    --output "${BEAT_MAP}" \
    --manifest-path "${MANIFEST_PATH}" \
    --manifest-handle "${MANIFEST_HANDLE}" \
    --attachment-state "${ATTACHMENT_STATE}"
else
  echo "[pipeline] Skipping beat map generation because SKIP_BEAT_MAP is set"
fi

if [ "${VERIFY_ATTACHMENTS}" != "0" ]; then
  echo "[pipeline] Verifying Gemini attachment handles..."
  "${PYTHON_BIN}" -m src.orchestrator.cli.check_gemini_handles \
    --state-file "${ATTACHMENT_STATE}" || true
fi

echo "[pipeline] Building core edit (beat map -> OTIO/FCPXML)..."
"${PYTHON_BIN}" -m src.orchestrator.cli.build_core_edit \
  --project-name "${PROJECT_NAME}" \
  --timeline-name "${TIMELINE_NAME}" \
  --segment-index "${SEGMENT_INDEX}" \
  --beat-map "${BEAT_MAP}" \
  --vo-audio "${VO_AUDIO}" \
  --manifest-path "${MANIFEST_PATH}" \
  --manifest-handle "${MANIFEST_HANDLE}" \
  --output-fcpxml "${FCPXML_PATH}" \
  --director-notes "${NOTES_PATH}" \
  --output-dir "${PROJECT_ROOT}/outputs"

echo "[pipeline] Importing FCPXML into Resolve..."
"${PYTHON_BIN}" -m gateway.resolve.automation import-fcpxml \
  --project "${PROJECT_NAME}" \
  --fcpxml "${FCPXML_PATH}" \
  --timeline "${TIMELINE_NAME}"

echo "[pipeline] Relinking timeline clips..."
"${PYTHON_BIN}" -m gateway.resolve.automation relink-timeline-clips \
  --project "${PROJECT_NAME}" \
  --notes-path "${NOTES_PATH}" \
  --report-path "${RELINK_REPORT}"

echo "[pipeline] Done. Timeline '${TIMELINE_NAME}' is ready in Resolve."
