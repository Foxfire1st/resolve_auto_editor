---
name: C-01-proto-edit
description: "Create a core edit using the instructions for beat analysis, harvesting, candidate ranking, and timeline assembly."
---

# C-01 Proto Edit

Create a core edit through four passes:

1. Beat visual analysis
2. Candidate harvesting
3. Candidate ranking / evidence comparison
4. Timeline assembly

## Pass A: Beat Visual Analysis

**Input:** voiceover beats  
**Output:** visual requirements (visual_requirements.md)

Translate the voiceover into what the viewer should see.

Example:

```text
"This setup is safer than it looks"
```

Becomes:

```text
show incoming threat + defensive mechanic + survival outcome
```

## Pass B: Candidate Harvesting

**Input:** beat visual requirement + indexed clip moments  
**Output:** broad list of possible matches (candidate_moments.json)

Find anything that might fit. This pass should be generous and high-recall.

Use any relevant retrieval signal, including:

- keyword search
- tags
- entities
- actions
- embeddings
- clip event summaries
- previous clip usage
- known character or mechanic names

Output example:

```json
{
  "beat_id": "B034",
  "candidate_moments": [
    "clip_017_m042",
    "clip_031_m011",
    "clip_044_m006",
    "clip_052_m018",
    "clip_076_m003"
  ]
}
```

At this point, it is acceptable if many candidates are mediocre. The failure mode to avoid is missing the good candidate.

## Pass C: Candidate Ranking / Evidence Comparison

**Input:** one beat visual requirement + harvested candidates  
**Output:** ranked evidence table (ranked_evidence_table.md)

Decide which candidate actually supports the beat.

This is where the LLM should be strict.

Answer:

- What visual evidence supports the beat?
- What is missing?
- Is the match direct, partial, generic, or wrong?
- Is the clip readable?
- Should this be used, rejected, or flagged as no-match?

## Pass D: Timeline Assembly

**Input:** selected/ranked moments + beat durations + confidence  
**Output:** Resolve edit decisions (e.g. FCPXML or OTIO)

Build the rough cut while handling pacing and continuity.

Consider:

- source in/out points
- target placement
- repeated clips
- clip continuation across beats
- when to hold a previous shot
- when to use B-roll, text, or overlay
- when to leave a manual-review placeholder

## Workflow Outline

```text
Voiceover beat
   ↓
A. Beat visual analysis
   ↓
B. Candidate harvesting
   ↓
C. Candidate ranking / evidence comparison
   ↓
D. Timeline assembly
   ↓
Resolve rough cut
```
