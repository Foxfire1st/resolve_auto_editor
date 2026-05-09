"""JSON schema definitions for structured Gemini outputs."""

from __future__ import annotations

from google.genai import types


def _string_schema() -> types.Schema:
    return types.Schema(type=types.Type.STRING)


def _float_schema() -> types.Schema:
    return types.Schema(type=types.Type.NUMBER)


def _bool_schema() -> types.Schema:
    return types.Schema(type=types.Type.BOOLEAN)


OBSERVATION_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "time_s": _float_schema(),
        "timecode": _string_schema(),
        "start_s": _float_schema(),
        "end_s": _float_schema(),
        "start_timecode": _string_schema(),
        "end_timecode": _string_schema(),
        "kind": _string_schema(),
        "text": _string_schema(),
        "details": _string_schema(),
        "labels": types.Schema(type=types.Type.ARRAY, items=_string_schema()),
        "confidence": _float_schema(),
    },
)


BOUNDARY_EVIDENCE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "start": _string_schema(),
        "end": _string_schema(),
        "anchor": _string_schema(),
        "notes": _string_schema(),
    },
)


EVENT_METADATA_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    required=["clip_summary", "events"],
    properties={
      "clip_summary": types.Schema(
          type=types.Type.OBJECT,
          required=["title", "description", "mode", "stage_name", "party", "enemies", "is_combat"],
          properties={
              "title": _string_schema(),
              "description": _string_schema(),
              "mode": _string_schema(),
              "stage_name": _string_schema(),
              "party": types.Schema(type=types.Type.ARRAY, items=_string_schema()),
              "enemies": types.Schema(type=types.Type.ARRAY, items=_string_schema()),
              "is_combat": _bool_schema(),
          },
      ),
      "filename_suggestion": _string_schema(),
      "tags": types.Schema(type=types.Type.ARRAY, items=_string_schema()),
      "events": types.Schema(
          type=types.Type.ARRAY,
          items=types.Schema(
              type=types.Type.OBJECT,
              required=["start_s", "end_s", "actor", "action_type", "details"],
              properties={
                  "time_s": _float_schema(),
                  "timecode": _string_schema(),
                  "start_s": _float_schema(),
                  "end_s": _float_schema(),
                  "anchor_s": _float_schema(),
                  "start_timecode": _string_schema(),
                  "end_timecode": _string_schema(),
                  "anchor_timecode": _string_schema(),
                  "actor": _string_schema(),
                  "actor_type": _string_schema(),
                  "action_type": _string_schema(),
                  "ability_name": _string_schema(),
                  "labels": types.Schema(type=types.Type.ARRAY, items=_string_schema()),
                  "targets": types.Schema(type=types.Type.ARRAY, items=_string_schema()),
                  "details": _string_schema(),
                  "notes": _string_schema(),
                  "observations": types.Schema(type=types.Type.ARRAY, items=OBSERVATION_SCHEMA),
                  "boundary_evidence": BOUNDARY_EVIDENCE_SCHEMA,
                  "confidence": _float_schema(),
              },
          ),
      ),
    },
)
