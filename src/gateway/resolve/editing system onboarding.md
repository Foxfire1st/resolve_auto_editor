# LLM‑Driven DaVinci Resolve Editing – Onboarding for gptCodex

This document is meant to be pasted into an IDE prompt to brief **gptCodex** (or any code‑gen LLM) about the architecture we want to build. The goal is that the model can read this once and then design an implementation plan and concrete modules.

---

## 0. Prerequisites & Environment Setup

1. **Install OpenTimelineIO** inside the repo virtualenv before running any OTIO/FCPXML tools:

  ```bash
  pip install opentimelineio==0.18.1
  ```

  (Already added to `pyproject.toml`; keep the runtime in sync.)

2. **DaVinci Resolve scripting**:

  * Resolve is installed under `C:\Program Files\Blackmagic Design\DaVinci Resolve`.
  * Ensure `DaVinciResolveScript` is reachable from the Resolve-provided Python interpreter or point `PYTHONPATH` to `Resolve\Developer\Scripting\Modules`.
  * **Important**: In `.env`, use absolute paths without quotes for `RESOLVE_SCRIPT_API` and `RESOLVE_SCRIPT_LIB` to ensure compatibility with `fuscript.exe`.
  * Refer to the [Resolve scripting reference](https://extremraym.com/cloud/resolve-scripting-doc/) for API capabilities and required permissions.

3. **Gemini ingest creds**: set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in `.env` so `gateway.vision.gemini_ingest` can describe rendered clips.

---

## 1. High‑Level Goal

We want an AI system that can:

1. **Assemble a rough cut automatically** in **DaVinci Resolve 20.x** from:

   * Aligned transcript / VO segments
   * Gameplay / b‑roll clips
   * Semantic metadata about clips
2. **Iterate on the edit** using:

   * Structural reasoning over a machine‑readable timeline
   * Visual + audio validation via a vision LLM (Gemini)
3. **Transition from structural editing to stylistic editing** without losing work:

   * Early phase: timelines are constructed and revised via **OpenTimelineIO (OTIO)** and re‑imported into Resolve.
   * Later phase: Resolve timeline becomes the source of truth; edits are driven by the Resolve scripting API and monitored via exports.

The orchestrating LLM is *not* hard‑coded here; think of it as an external brain that talks to our services via JSON.

Your job (gptCodex) will be to help design and implement the code that:

* Manages the timeline representations (OTIO, Resolve exports, internal JSON)
* Bridges between LLM requests and DaVinci Resolve / Gemini
* Keeps the system consistent across multiple edit iterations

---

## 2. Core Concepts & Terminology

### 2.1 Two Editing Phases

We explicitly separate the workflow into **two phases**.

#### Phase 1 – OTIO Mode (Structural Editing)

* OTIO timeline is the **source of truth**.
* We build a `otio.schema.Timeline` from LLM‑provided JSON.
* We export to **FCPXML 1.10** using OTIO’s FCPXML adapter.
* DaVinci Resolve imports that FCPXML to create a timeline.
* We can repeatedly regenerate and re‑import the whole timeline (rough cut v1, v2, v3, …).
* No styling or effects that OTIO / FCPXML can’t represent should be considered “final” in this phase.

This phase is for:

* Clip selection
* Ordering
* Durations
* Simple track layout
* B‑roll placement
* Gap / pacing adjustments

#### Phase 2 – Resolve Mode (Styling & Late Structural Changes)

Once we’re happy with the **structure** of the rough cut, we “lock” the OTIO phase:

* DaVinci Resolve’s own timeline becomes the **authoritative state**.
* We stop regenerating from OTIO.
* We export the Resolve timeline as **`.drt` (DaVinci Resolve Timeline)** for machine reading.
* We use **DaVinci Resolve’s Python scripting API** to:

  * Insert clips
  * Perform ripple inserts
  * Add transitions
  * Adjust crop/transform where possible
* Visual QA is done by rendering segments or the full timeline and sending them to Gemini via our **`gemini_ingest`** system.

This phase is for:

* Transitions
* Cropping/reframing
* Overlays & chapter cards
* Text, graphics, Fusion elements
* Audio mixing & fine adjustments
* Late ripple inserts (e.g., chapter bumpers)

Once in Phase 2 we **never overwrite the timeline via OTIO again**. We only make incremental changes inside Resolve.

### 2.2 Representations of the Timeline

We will have three key representations of the edit:

1. **Semantic Edit JSON** (LLM‑facing)

   * High‑level description of intended structure.
   * Expressed in terms of narration segments, clip IDs, approximate timings, and semantic labels.
   * Used primarily in Phase 1, but some concepts may carry over.

2. **OTIO Timeline** (Phase 1 source of truth)

   * A concrete, programmatic representation using `opentimelineio` objects.
   * Contains tracks, clips, gaps, transitions (where supported), time ranges.
   * Exportable to FCPXML 1.10 for Resolve import.

3. **Resolve Timeline Export (`.drt`)** (Phase 2 source of truth)

   * Native Resolve timeline export (XML under the hood).
   * Contains **everything** Resolve knows about the sequence:

     * Clip placements
     * Transitions
     * Effects & Fusion comps (by reference)
     * Crops, transforms
     * Audio settings, automation
   * We treat this as **read‑only input**; we never try to write or patch `.drt` directly.

Additionally, we maintain an **internal normalized JSON timeline model** derived from either OTIO (Phase 1) or DRT exports (Phase 2) that the LLM can reason over.

### 2.3 Visual QA via `gemini_ingest`

There is an existing `gemini_ingest` pipeline (under something like `src/gateway/vision/` in the repo) that:

* Accepts uploaded video segments.
* Optionally **slows them down** (1x–5x stretch) to increase effective temporal resolution for Gemini (since Gemini typically samples at ~1 fps).
* Sends them to a Gemini vision model to obtain **timestamped descriptions** of what’s happening on screen.
* Returns JSON summaries keyed by timestamps.

The orchestrating LLM can:

* Ask for a render of a section of the timeline (first 30s, a particular chapter, etc.).
* Use `gemini_ingest` results to verify that the visuals match the narration and its own plan.
* Request bulk or targeted structural or stylistic changes based on that feedback.

---

## 3. High‑Level Components

This section is about the modules we likely need. gptCodex can refine or reorganize them.

### 3.1 Orchestrator API Layer

**Responsibility:** Provide a clean, LLM‑friendly API over HTTP or a local RPC mechanism.

Rough responsibilities:

* Accept **semantic edit plans** (JSON) from the orchestrating LLM.
* Trigger OTIO timeline builds and Resolve imports.
* Trigger renders of specific timeline ranges.
* Trigger `gemini_ingest` analysis.
* Expose endpoints to get the current **timeline snapshot JSON** (derived from OTIO or DRT, depending on phase).
* Expose operations for structural / stylistic changes ("insert bumper after chapter 3", "tighten clip at t=05:12 by 0.5s", etc.).

This layer should not know the nitty‑gritty details of OTIO or Resolve; it should delegate to specialized modules.

### 3.2 OTIO Engine (Phase 1)

**Responsibility:** Translate semantic JSON into an OTIO timeline, and vice versa.

Core tasks:

* Define a mapping between **Semantic Edit JSON** and OTIO constructs (timeline, tracks, clips, gaps).
* Provide functions:

  * `build_otio_from_semantic_plan(plan_json) -> otio.schema.Timeline`
  * `export_otio_to_fcpxml(otio_timeline, path)`
  * `import_fcpxml_to_otio(path) -> otio.schema.Timeline` (optional, may not be needed if we treat OTIO as one‑way for the MVP).
* Provide a **normalized JSON snapshot** view derived from OTIO (`otio_to_snapshot_json`).

Semantic JSON should be designed so that LLM manipulations are straightforward (e.g., a list of segments with pointers to media assets, desired timing, etc.).

### 3.3 Resolve Bridge (Phase 1 + Phase 2)

**Responsibility:** Wrap the **DaVinci Resolve Python scripting API** in a safe, higher‑level interface.

Key capabilities:

**Phase 1:**

* Import FCPXML generated by OTIO to create a new timeline.
* Optionally delete/replace existing rough‑cut timeline when re‑importing.

**Phase 2:**

* Export the active timeline as `.drt` to a workspace directory.
* Provide programmatic operations like:

  * `render_range(timeline, start_timecode, end_timecode, output_path)`
  * `insert_clip_ripple(timeline, media_pool_clip_id, track_index, record_timecode)`
  * `add_transition(timeline, track_index, at_timecode, transition_name, duration)`
  * `get_timeline_items(timeline, track_index, type="video"|"audio")`
  * `apply_basic_transform(timeline_item, crop/zoom params)` (where supported by API)
* Keep all Resolve API calls encapsulated so the rest of the system never needs to talk to Resolve directly.

We should assume Resolve scripting is running in a **local Python environment** launched from Resolve, with our project loaded.

### 3.4 Timeline Snapshot & DRT Parser (Phase 2)

**Responsibility:** Turn exported `.drt` files into a normalized JSON snapshot for the LLM.

* Input: path to `.drt` (Resolve timeline export)
* Process: parse XML, extract relevant structure:

  * Tracks (video, audio)
  * For each clip: asset ID/path, in/out, record time range, track index
  * Transitions: type, duration, between which clips
  * Basic transforms (scale, crop, position), where straightforward
  * Markers, chapter markers, etc.
* Output: JSON structure like `TimelineSnapshot`, e.g.:

```json
{
  "fps": 60,
  "duration": "00:20:13:12",
  "video_tracks": [
    {
      "index": 1,
      "items": [
        { "id": "clip_001", "src": "...mp4", "start": "00:00:00:00", "end": "00:00:05:12", "record_in": "00:00:00:00", "record_out": "00:00:05:12", "has_transition_out": true, "transition_out_type": "Cross Dissolve" }
      ]
    }
  ],
  "audio_tracks": [ ... ]
}
```

This snapshot is what the orchestrating LLM will mostly read and reason about in Phase 2.

**Important:** we never modify `.drt` files. They are parsed -> converted to JSON -> discarded.

### 3.5 Vision Gateway (`gemini_ingest`)

**Responsibility:** Provide a programmable way to send renders to Gemini and obtain structured feedback.

Key capabilities (some already exist, we just need to adapt them):

* Accept a local video file path plus options: slowdown factor (1–5x), segmenting, etc.
* Upload to storage (e.g., GCS) and send prompts to Gemini (Gemini 3 Pro, etc.).
* Obtain JSON describing:

  * Per‑timestamp textual description of what’s on screen.
  * Optional event markers (e.g., “character appears”, “UI shows stats”, etc.).
* Allow follow‑up queries over the same stored asset while it’s still resident.

The orchestrator will use this primarily for:

* Post‑hoc validation after an edit pass.
* Targeted investigations of problem segments.

---

## 4. LLM Interaction Model

We assume **two kinds of LLMs**:

1. **Orchestrating LLM** ("director" brain)

   * Does high‑level reasoning about structure and quality.
   * Issues operations to our API: "build rough cut", "insert chapter bumper", "tighten segment 03 by 0.7s", etc.
   * Reads timeline snapshots and Gemini reports.

2. **Code‑gen LLM (gptCodex)**

   * Helps us implement the code and infrastructure described above.
   * Once the system exists, it may also assist with writing new tools, but that’s secondary.

### 4.1 Semantic Edit JSON (Phase 1)

We should define a JSON schema that looks roughly like this (gptCodex can refine it):

```json
{
  "project_id": "video_foxian_prayer_guide_v1",
  "fps": 60,
  "segments": [
    {
      "id": "seg_001",
      "narration_span": { "start_sec": 0.0, "end_sec": 8.0 },
      "intent": "Intro: explain Foxian Prayer effect.",
      "primary_clip": { "asset_id": "main_talking_head", "src_in": 12.0, "src_out": 20.0 },
      "broll": [
        {
          "asset_id": "gameplay_overlay_1",
          "src_in": 30.0,
          "src_out": 35.0,
          "record_offset": 2.0,
          "opacity": 1.0
        }
      ],
      "chapter_bumper_after": false
    }
  ]
}
```

This is what the orchestrating LLM would send to the OTIO engine to build an initial timeline.

### 4.2 Operations in Phase 2

Instead of trying to edit raw `.drt`, the LLM should:

1. Request a **timeline snapshot JSON** from the orchestrator.
2. Request **renders + Gemini reports** for specific time ranges.
3. Compute changes it wants (e.g., ripple insert a chapter card at 10:30, shorten a clip by 0.7s, add a transition, etc.).
4. Call high‑level operations exposed by the orchestrator API:

   * `POST /resolve/insert_bumper` with details.
   * `POST /resolve/trim_clip` with clip id and new out point.
   * `POST /resolve/add_transition`.
5. Ask for a new snapshot & (optionally) new Gemini validation.

The implementation of those endpoints will be backed by the Resolve Bridge + DRT parser described earlier.

---

## 5. Key Invariants & Constraints (Important for gptCodex)

When implementing this system, **keep these invariants** in mind:

1. **OTIO is authoritative only in Phase 1.** Once we declare a structural lock, OTIO timelines are historical only.
2. **Never mutate `.drt` directly.** Treat it as read‑only exported XML; always convert to JSON and then drive changes via Resolve’s scripting API.
3. **Only one active Resolve timeline for the project.** To avoid performance issues and complexity, we generally overwrite or update one designated "AI Edit" timeline instead of accumulating 40 tabs.
4. **Phase boundary is explicit.** There should be a clear flag / state machine indicating whether a project is in OTIO mode or Resolve mode.
5. **All external state changes are idempotent where possible.** Replaying the same request should either:

   * have no additional effect, or
   * be detected and handled gracefully.
6. **Gemini analysis is advisory, not authoritative.** The timeline structure is still defined by OTIO/Resolve; Gemini just helps detect mismatches and guide edits.

---

## 6. What We Want gptCodex To Do Next

Given this document, gptCodex should:

1. **Propose an implementation plan** broken down into milestones, for example:

   * Milestone 1: OTIO engine + FCPXML export and manual Resolve import.
   * Milestone 2: Resolve Bridge for import/export/render and basic API server.
   * Milestone 3: DRT parser and normalized snapshot JSON.
   * Milestone 4: Integrate `gemini_ingest` for validation.
   * Milestone 5: High‑level orchestrator API with a few core operations.
2. **Design concrete Python modules and packages**, including naming and basic file structure.
3. **Define the initial Semantic Edit JSON schema** and the internal Timeline Snapshot JSON schema.
4. **Sketch the key functions / classes**, with docstrings describing responsibilities.
5. Optionally, **generate starter code** for:

   * OTIO builder
   * Resolve bridge stubs
   * DRT → JSON parser skeleton
   * A minimal FastAPI/Flask API for the orchestrator.

The focus at this stage is on **clean architecture and clear interfaces**, not on finishing every edge case.

---

You (gptCodex) can assume:

* Python is the primary language for backend / scripting.
* DaVinci Resolve scripting is available via `DaVinciResolveScript` in Python.
* `opentimelineio` needs to be added to the project and installed.
* There is an existing `gemini_ingest` module to integrate with (we can adapt its API as needed).

Use this document as your onboarding brief and then propose a detailed implementation plan.

---

## 7. Current Gaps Before Coding the Roundtrip

1. **Semantic edit schema + OTIO builder**: need dataclasses/Pydantic models plus helpers in `gateway/otio_fcpxml` that place clips (including linked audio) on explicit track indices and export FCPXML 1.10.
2. **Resolve bridge**: wrappers under `gateway/resolve` to import FCPXML, select the `feixiao_test_edit` project timeline, trigger Deliver renders, and export `.drt` snapshots.
3. **Phase/state tracking**: a persistent flag indicating OTIO vs Resolve mode so the orchestrator knows when to stop regenerating timelines.
4. **Timeline snapshots**: converters for both OTIO (Phase 1) and `.drt` XML (Phase 2) that emit the normalized JSON consumed by the orchestrator.
5. **Gateway endpoints/tests**: FastAPI handlers and unit tests for `build_timeline`, `import_timeline`, render/export operations, and Gemini-powered description requests.
6. **Gemini validation wiring**: leverage the existing `gemini_ingest` module as the “eyes” of the orchestrator by feeding rendered clips plus neutral prompts (e.g., “Describe what you see, what this is about, who appears”).

Track these gaps as you implement the minimal roundtrip so nothing gets skipped when moving from OTIO-only experiments to Resolve-authoritative edits.

## Davinci Resolve Scripting API - Documentation:
https://extremraym.com/cloud/resolve-scripting-doc/