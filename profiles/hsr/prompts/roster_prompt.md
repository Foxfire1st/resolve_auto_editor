You are assisting with a Honkai: Star Rail ingest pipeline. Go through the entire clip and identify the four playable
characters on the player's roster and all visible enemies using their canonical English names.

Respond with JSON only in the following structure:
{
  "roster": ["Character A", "Character B", "Character C", "Character D"],
  "enemies": ["Enemy 1", "Enemy 2", "Enemy 3", ...],
  "notes": "<optional uncertainty or context>",
  "mode_hint": "<optional text about the mode/stage>"
}

- Never invent names that are not visible in the UI. Stick to what is rendered in the clip.
