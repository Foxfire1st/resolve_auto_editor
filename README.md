*** WIP: This repo is experimental and only held together by thoughts and prayers. But if you find this interesting I am happy to get feedback. ***

# Auto-Editor

Local-first automation stack for DaVinci Resolve: gateway APIs, OTIO/FCPXML tooling, retrieval/orchestrator agents, and a minimal approval UI. See `auto_editor_engineering_brief.md` for the detailed MVP-to-production plan.

## Repo Map

```
.
├─ .env                          # Resolved API keys + Resolve paths (checked-in template)
├─ settings.json                 # Global toggles such as active profile
├─ profiles/
│   └─ hsr/
│       ├─ profile.json          # Manifest for prompts, hooks, scripts
│       ├─ prompts/              # Markdown prompt snippets per media type
│       ├─ scripts/              # Domain-only helpers (e.g., roster extraction)
│       └─ resources/            # Cached game data (characters, items, etc.)
├─ samples/
│   └─ feixiao test edit/        # Canonical demo project + renders/DRTs stay under timeline/
|       └─ outputs/              # Captured ingest/alignment JSONs for regression review
├─ src/
│   ├─ gateway/                  # FastAPI gateway + Resolve/OTIO/FCPXML helpers
│   │   ├─ audio/                # VO alignment CLI + helpers
│   │   ├─ vision/               # Gemini ingest, VideoPrism, timecode math
│   │   └─ catalog/              # Scene segmentation + search contracts
│   ├─ orchestrator/             # DeepSeek/Kimi tool loops, prompts, retrieval wiring
│   ├─ services/                 # Shared services (encoding, caching, etc.)
│   └─ ui/                       # Minimal approval/nudge web UI
├─ scripts/
│   └─ *.sh                      # Collection of shell scripts to run complete cli-workflows for development purposes
├─ tools/
│   └─ ffmpeg/                   # Pinned ffmpeg builds used by encoding service
└─ auto_editor_engineering_brief.md  # Task cards + long-form plan
```

## Getting Started
1. Edit `.env` (tracked in repo as the template) and fill in Resolve + API paths before running any CLIs.
2. Install dependencies: `.\.venv\Scripts\python.exe -m pip install -e .[dev]` on Windows, or `./.venv/bin/python -m pip install -e '.[dev]'` on POSIX shells.
3. Run tests: `.\.venv\Scripts\python.exe -m pytest` on Windows, or `./.venv/bin/python -m pytest` on POSIX shells.

### Runtime boundaries
- **Repo helpers, OTIO builders, tests, FastAPI services** → always run with the repo virtualenv interpreter: `.\.venv\Scripts\python.exe` on Windows or `./.venv/bin/python` on POSIX shells. Use module invocations through that interpreter for installs and tests. Do not use active PATH Python or install project dependencies globally.
- **Resolve automation** → always run inside Resolve’s own Python runtime using `fuscript.exe` (or Resolve’s Scripts menu). Those scripts inherit `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB`, and `PYTHONPATH` from `.env`. Do **not** activate `.venv` when launching Resolve-side scripts—Resolve ships its own interpreter and module set.
- **Environment Configuration**: Ensure your `.env` file uses absolute paths without quotes for Resolve settings. This is critical for `fuscript` compatibility.
  ```ini
  # Correct .env example
  RESOLVE_SCRIPT_API=C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
  RESOLVE_SCRIPT_LIB=C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
  PYTHONPATH=%PYTHONPATH%;%RESOLVE_SCRIPT_API%\Modules
  ```

For command examples below, set `PY` to the repo virtualenv interpreter first:

```bash
PY=.venv/Scripts/python.exe   # Windows Git Bash
# PY=./.venv/bin/python       # POSIX shells
```

### Programmatic roundtrip helpers
- `orchestrator.tool_bridge.otio_tools` offers `build_timeline_plan` and `build_and_export_fcpxml` so agents can turn JSON-friendly clip specs into FCPXML without touching sample-specific paths.
- `orchestrator.tool_bridge.resolve_tools.ResolveTools` wraps the Resolve bridge with `import_fcpxml`, `render`, and `export_timeline_archive` methods so the orchestrator can compose automations project-by-project.

### Building a focused Core Edit slice
- Run `"$PY" -m orchestrator.cli.build_core_edit --time-range 00:05:12-00:06:31 --output-fcpxml "samples/feixiao test edit/outputs/core_edit.fcpxml" --director-notes "samples/feixiao test edit/outputs/director_notes_focus.json"` to drive the production planner/exporter on a narrow narration window. This goes through the event metadata retrieval path with no manual overrides, so Resolve sees the exact structure that ships in production.

### Inspecting beats & shot planning
- After a pipeline run, use `"$PY" -m orchestrator.cli.inspect_beat --beat-id B003 --beat-map samples/feixiao\ test\ edit/script_breakdown.json --segment-index samples/feixiao\ test\ edit/alignment_segments.json` to print the script breakdown intent, narration spans, saved shot plan, per-shot retrieval traces (from `outputs/shot_debug/`), and the corresponding director notes. Adjust the paths/beat ID as needed to audit other sections of the timeline.

### Installing the Resolve launcher

DaVinci Resolve only exposes its scripting API to Python files that live under
`%PROGRAMDATA%/Blackmagic Design/DaVinci Resolve/Fusion/Scripts`. Use the helper
below to drop a tiny launcher there which imports this repo and calls the
minimal round-trip entry point:

1. Ensure your virtual environment is active so `dotenv` and other dependencies
   are on `sys.path`.
2. Prepare the roundtrip assets once (or whenever footage changes):
   ```powershell
   .\.venv\Scripts\python.exe scripts/dev/build_roundtrip_assets.py
   ```
   This builds `samples/feixiao test edit/timeline/roundtrip.fcpxml` using the Feixiao sample footage so the
   Resolve half never needs `opentimelineio`.
3. To drive Resolve from outside the UI, invoke your automation through
   `fuscript.exe` (Resolve must be running):
   ```powershell
   .\.venv\Scripts\python.exe scripts/dev/run_resolve_via_fuscript.py
   ```
   - Override `--fuscript` if Resolve lives in a non-default location.
   - Use `--script` to target another Resolve-only script, or `--script-arg` to
     pass parameters through.
   The helper reads `.env` so `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB`, and
   `PYTHONPATH` lines stay the single source of truth.
4. Run the Fusion installer if you also want the menu entry inside Resolve:
   ```powershell
   .\.venv\Scripts\python.exe scripts/dev/install_resolve_launcher.py
   ```
   - Use `--launcher-name` to change the filename written under
     `Fusion/Scripts/Utility/`.
   - Use `--entry package.module:function` if you need a different entry point
     than `scripts.dev.minimal_roundtrip:main`.
5. Open Resolve and trigger the script from `Workspace > Scripts > Utility >
   auto_editor_launcher`.

The generated launcher adds both the repo root and your active virtualenv’s
site-packages directory to `sys.path`, loads `.env`, and then executes the
requested entry function—keeping full control inside this codebase while using
Resolve’s embedded interpreter.

Task cards (T1–T9) live in `auto_editor_engineering_brief.md`; work on one card per iteration and keep modules decoupled.

> **Project locality.** All Resolve artifacts that come out of the round-trip flow—`roundtrip.fcpxml`, `testrender.mp4`, `*.drt`, etc.—should stay under `samples/feixiao test edit/`. Keeping every render, project file, and script asset inside the canonical sample folder mirrors the way Resolve organizes project media and prevents orphaned files elsewhere in the repo.

## VO ↔ Script Alignment

The alignment workflow is now transcript-first:

1. Run the `align` CLI once; it always emits a word-level transcript next to your output JSON
    (`samples/feixiao test edit/alignment_60s.transcript.json` by default, or the path you pass via `--transcript-out`).
2. Inspect/edit that transcript file as needed.
3. Re-run alignment against the saved transcript to skip Whisper entirely:

```bash
# first run: record a transcript and alignment together
"$PY" -m gateway.audio.vo_script_alignment.cli \
   --script "samples/feixiao test edit/script.md" \
   --audio "samples/feixiao test edit/audio/feixiao_vo_60s.wav" \
   --out "samples/feixiao test edit/alignment_60s.json"

# subsequent runs reuse the saved transcript (no Whisper pass)
"$PY" -m gateway.audio.vo_script_alignment.cli \
   --script "samples/feixiao test edit/script.md" \
   --transcript "samples/feixiao test edit/alignment_60s.transcript.json" \
   --audio "samples/feixiao test edit/audio/feixiao_vo_60s.wav" \
   --out "samples/feixiao test edit/alignment_60s.json"
```

The aligner trusts the transcript’s common words and automatically substitutes any remaining
misheard names via process-of-elimination, so you can drop the giant YAKE term lists unless you
need an explicit prompt.

### Working with large alignment files

Use `gateway.audio.vo_script_alignment.alignment_helpers` to stream the JSON back out without
loading/serializing everything by hand:

```python
from pathlib import Path
from gateway.audio.vo_script_alignment import alignment_helpers as helpers

alignment_path = Path("samples/feixiao test edit/alignment_full.json")

# Iterate span-by-span (good for feeding an LLM sequentially)
for span in helpers.iter_spans(alignment_path):
   print(span.id, span.text, span.t0, span.t1)

# Pull just the content that overlaps the first 15 seconds
first_chunk = helpers.spans_in_window(alignment_path, start_time=0.0, end_time=15.0)

# Slide a window across the VO in 20-second chunks
for window_start, window_end, spans in helpers.iter_time_windows(alignment_path, window_seconds=20.0, stride_seconds=10.0):
   print(window_start, window_end, len(spans))
```

These helpers keep the orchestrator’s token footprint predictable: the LLM can request spans for a
time range, reason about them, and move on without ever ingesting the entire 18k-line file at once.

### Regenerating beat maps

Use the new CLI whenever you need to rebuild `script_breakdown.json` from a narration alignment and
script:

```bash
"$PY" -m src.orchestrator.cli.generate_beat_map \
   --segments "samples/feixiao test edit/alignment_segments.json" \
   --script "samples/feixiao test edit/script.md" \
   --profile-text "profiles/hsr_guide/prompts/guide_analysis_prompt.md" \
   --output "samples/feixiao test edit/script_breakdown.json"
```

Set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in `.env` before running the command; the generator fails
fast if the key is missing since beats must come from the LLM.

### End-to-end Resolve pipeline

After the beat map and embeddings are ready, run the full automation loop (build OTIO → export
FCPXML → Resolve import → relink) via:

```bash
bash src/orchestrator/cli/run_core_pipeline.sh
```

Override environment variables to adapt paths without editing the script, e.g.,

```bash
PROJECT_NAME="Guide Auto-Edit" TIMELINE_NAME="Core Edit QA" bash src/orchestrator/cli/run_core_pipeline.sh
```

Make sure Resolve is open with scripting enabled before running the pipeline.

## Gemini ingest for footage folders

`gateway.vision.gemini_ingest` now batches every clip under `footage/regular` and
`footage/slowdown`, slows only the combat folder to 5× (≈200 ms sampling), and writes
each Gemini 2.5 Flash response into the sibling `meta/` directory. The CLI automatically
chooses prompts + hook sets per clip based on your active profile’s media-type
definitions and the project’s `project.settings.json` mapping.

```bash
"$PY" -m gateway.vision.gemini_ingest.cli \
   --footage-root "samples/feixiao test edit/footage" \
   --batch-meta-folder-name meta \
   --slowdown-folder-factor 5.0 \
   --overwrite   # optional: re-run even if meta already has JSON
```

- `regular/` clips stay at 1× slowdown; `slowdown/` uses the 5× combat profile.
- Set `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) in the project `.env`; the CLI reads it from there, so double-check that file exists before running `gemini_ingest`.
- Outputs land in `regular/meta/<clip>_gemini.json` and `slowdown/meta/<clip>_gemini.json`.
- The CLI sticks with `gemini-2.5-flash` unless you override `--model` or `.env`.
- Use `--batch-video-extensions` or rename the folders via `--regular-folder-name` /
   `--slowdown-folder-name` if you need a different layout.
- Resume a partial run with `--batch-clip slowdown/mozerobinAS.mp4 --batch-clip slowdown/topazRMAS.mp4`
   to process only the missing combat clips (existing JSON files are skipped unless `--overwrite`).

You can still analyze a single clip with `--video-path` as before; batch mode simply automates the
standard Resolve ingest workflow for the Feixiao test set.

### Media-type aware prompts & hooks

Profiles declare video media types in `profiles/<name>/profile.json`. Each type points to a list of
prompt keys and hook IDs; `gemini_ingest` loads those sections at runtime and concatenates the
prompt files in order. Example (`profiles/hsr/profile.json`):

```json
"media_types": {
   "video": [
      { "type": "combat", "prompts": ["timeline", "roster"], "hooks": ["ability_loader"], "slowdown": 5.0 },
      { "type": "non_combat", "prompts": ["utility", "roster"], "hooks": ["ability_loader"], "slowdown": 1.0 }
   ]
}
```

Projects map folders (or absolute paths) onto those media types via a `project.settings.json`
file placed anywhere along the footage path. The ingest pipeline walks up parent directories from
each clip until it finds this file:

```json
{
   "media_types": {
      "video": {
         "combat": ["footage/slowdown"],
         "non_combat": ["footage/regular"]
      }
   }
}
```

Keep prompts domain-specific within the profile (e.g., `prompts/utility_prompt.md` for ambient or
non-combat clips) while the shared pipeline stays neutral. To add new types, drop the prompt
markdown under `profiles/<name>/prompts/`, register it inside `profile.json`, and update the project
settings file to route folders into the new type. Hooks follow the same pattern—only the
listed IDs run for a given media type, so you can keep combat-only ability lookups separate from
exploratory footage summaries. Optional `slowdown` values on media types override both CLI flags and
the `.env` defaults, so combat clips can be forced to 5× slowdown even if the operator forgets the
batch flag.

## Event Metadata
Event metadata retrieval code lives in `src/gateway/vision/event_metadata/`. It stores clip-level metadata and editor-usable visual event spans in a profile-local SQLite database, defaulting to `profiles/<profile>/data/event_metadata.db`.

### Gemini Files API quick reference
- The Gemini Files API lets us stage up to **20 GB** of data per project with a **2 GB per-file** ceiling, and Google retains each upload for roughly **48 hours** before automatic deletion ([docs](https://ai.google.dev/gemini-api/docs/files)).
- Because uploads persist across requests, we can ship large assets (full clip manifests, beat-map archives, VO transcripts, thumbnails) once per pipeline run and then point multiple LLM calls at the same file handles instead of inlining megabytes of context.
- Treat uploaded manifests as the canonical “footage lookup table”: the beat-map generator, shot planner, and any downstream agent can reference the same file IDs, which keeps prompts short and avoids hard limit caps on summaries (or other relevant data) that existed when we had to stuff data directly into the request body.
- When the 48 hour TTL expires, simply re-upload the manifest as part of the pipeline warm-up step (e.g., right after ingest or before running `run_core_pipeline.sh`). This keeps storage usage predictable and means we never rely on stale descriptions.
- **File-backed manifest workflow:**
  1. Export the ingest summaries into a newline-delimited manifest once per ingest:
     ```bash
     "$PY" -m src.orchestrator.cli.build_footage_manifest \
        --project-settings samples/feixiao\ test\ edit/project.settings.json \
        --output samples/feixiao\ test\ edit/outputs/footage_manifest.ndjson
     ```
  2. Generate beats while uploading (and caching) the manifest file. The CLI always requires a manifest path and writes `footage_manifest.handle.json` with the Gemini file name + TTL so future steps can reuse it:
     ```bash
     "$PY" -m src.orchestrator.cli.generate_beat_map \
        --segments samples/feixiao\ test\ edit/alignment_segments.json \
        --script samples/feixiao\ test\ edit/script.md \
        --profile-text profiles/hsr_guide/prompts/guide_analysis_prompt.md \
        --manifest-path samples/feixiao\ test\ edit/outputs/footage_manifest.ndjson \
        --manifest-handle samples/feixiao\ test\ edit/outputs/footage_manifest.handle.json \
        --attachment-state samples/feixiao\ test\ edit/outputs/gemini_attachments.json
     ```
  3. When building the Core Edit, provide the same manifest path/handle so the shot planner attaches that file instead of falling back to inline summaries:
     ```bash
     "$PY" -m src.orchestrator.cli.build_core_edit \
        --beat-map samples/feixiao\ test\ edit/script_breakdown.json \
        --segment-index samples/feixiao\ test\ edit/alignment_segments.json \
        --manifest-path samples/feixiao\ test\ edit/outputs/footage_manifest.ndjson \
        --manifest-handle samples/feixiao\ test\ edit/outputs/footage_manifest.handle.json
     ```
  The handle is only valid if the local manifest file still matches (size + mtime) and the 48 hour TTL has not elapsed; otherwise the CLI automatically re-uploads and refreshes the metadata file.
- `src/orchestrator/retrieval/attachment_state.py` writes `gemini_attachments.json`, a shared registry keyed by logical attachment names. Every upload CLI updates this file so other tools (e.g., `ShotPlanner`) can discover current handles without guessing paths.
- Use `"$PY" -m src.orchestrator.cli.upload_attachment --name alignment_windows --file samples/.../alignment_windows.json --state-file samples/.../outputs/gemini_attachments.json --reuse` to stage additional assets such as alignment windows. Handles are saved next to the source file and referenced in the state registry.
- Validate cached handles before running automations via `"$PY" -m src.orchestrator.cli.check_gemini_handles --state-file samples/.../outputs/gemini_attachments.json`. The command reports whether each attachment is `valid`, `stale` (file changed/expired), or `missing`.
- `scripts/run_core_pipeline.sh` always uses the manifest workflow and automatically uploads/attaches the NDJSON file. Override behavior with env vars:
   - `BUILD_MANIFEST=0` skips the pre-build step if you already have a fresh manifest on disk.
   - `VERIFY_ATTACHMENTS=1` runs the validation CLI mid-pipeline and logs any stale handles.

3. Ingest a clip for the default `hsr` profile after Gemini ingest has produced a matching `*_gemini.json` artifact:

   ```bash
   "$PY" -m gateway.vision.event_metadata.cli ingest \
     --video-path path/to/clip.mp4 \
     --gemini-json path/to/meta/clip_gemini.json \
     --profile hsr
   ```

4. Batch-ingest footage folders declared by a project:

   ```bash
   ./scripts/run_ingest.sh "samples/feixiao test edit"
   ```

5. Run a sample search:

   ```python
   from gateway.vision.event_metadata.search import EventSearchFilters, search_events

   results = search_events(
       "boss gets weakness broken and Seele is on field",
       profile_name="hsr",
       filters=EventSearchFilters(required_tags=["#seele", "#floor12"]),
   )
   ```

   ### Event metadata search CLI

   When you want a high-level view of stored event spans that match a prompt, run:

   ```bash
   ./scripts/search_segments.sh "Feixiao idle animation" --max-results 10
   ```

   The script activates `.venv`, queries `gateway.vision.event_metadata.cli search`, and prints trusted event spans with known start/end seconds. Use `--video-contains <substring>` to constrain candidates or `--profile <name>` to switch profiles.
