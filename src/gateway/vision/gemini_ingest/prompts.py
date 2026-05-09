"""Generic fallback prompts for Gemini ingest.

Profile-specific prompts live under ``profiles/<profile>/prompts`` and should be
preferred for normal project behavior.
"""

DEFAULT_TIMELINE_PROMPT = """
You are analyzing source footage for a video editing pipeline. Produce a precise
JSON event log based only on visible and audible evidence in the clip.

For each meaningful visual or UI event, include start/end seconds, a concise
actor when visible, a generic action type, any readable labels, and enough
details for an editor to understand why the moment is useful.

Respond with JSON only.
""".strip()


DEFAULT_ROSTER_PROMPT = """
Identify the main visible participants, subjects, or entities in this clip.
Use names only when they are visible in the UI or otherwise directly evidenced.

Respond with JSON only in this structure:
{
  "roster": ["Visible subject A", "Visible subject B"],
  "enemies": ["Visible opposing subject A"],
  "notes": "<optional uncertainty or context>",
  "mode_hint": "<optional text about the clip mode or setting>"
}
""".strip()
