---
name: C-02-evolutionary-edit
description: "Iteratively improve a rough core edit through strict beat-by-beat visual planning, per-visual candidate harvesting, per-visual evidence ranking, timeline assembly, quality review, and Resolve feedback. Use when a C-01 proto edit is too coarse, when edit quality must improve over successive versions, or when Codex must produce or revise a Resolve rough cut from voiceover, footage skims, shot plans, assembly proposals, director notes, review reports, or FCPXML/OTIO artifacts."
---

# C-02 Evolutionary Edit

This skill improves edit quality by repeating the C-01 edit workflow at a stricter granularity. The core unit is not "all clips in the project" and not even "one beat equals one clip." The core unit is:

```text
beat -> visual span -> candidate clip slices -> ranked evidence -> timeline placement
```

Every pass must preserve artifacts. Every editorial choice must be traceable from the voiceover beat, to the planned visual span, to candidate source ranges, to ranked evidence, to the timeline assembly row.

## Non-Negotiable Workflow

```text
Voiceover beat
   v
A. Beat visual analysis
   v
B. Candidate harvesting
   v
C. Candidate ranking / evidence comparison
   v
D. Timeline assembly
   v
Resolve rough cut
```

Do these steps for every beat, and for every visual span inside that beat. Do not create a global ranked list of clips. A ranked evidence table is only useful if each row answers: "For this exact beat visual, which candidate clip slice best fills this exact timeline span, and why?"

Work one phase at a time and then ask for feedback.

## Output Contract

Write each iteration to a new versioned folder such as:

```text
outputs/evolutionary_edit/v001/
outputs/evolutionary_edit/v002/
```

Never overwrite a previous generation unless the user explicitly asks for replacement.

Required artifacts:

```text
iteration_manifest.json
visual_requirements.md
candidate_moments.json
ranked_evidence_table.md
timeline_assembly.md
assembly_quality_report.md
change_brief.md
feedback_log.md
review_notes.md
```

Optional artifacts, when the edit decisions are ready for interchange or the user allows conversion:

```text
assembly_proposal.json
director_notes.json
director_notes.md
semantic_edit.json
rough_cut.fcpxml
rough_cut.otio
resolve_feedback.json
```

`iteration_manifest.json` records the input artifact paths, source data paths, generated artifact paths, review status, and previous iteration compared against.

## Pass A Part 1: Beat Visual Analysis

Read one voiceover beat at a time. Split it into one or more visuals that should appear on screen. Each visual must have a target timeline range inside the beat.

The artifact is `visual_requirements.md`.

Each visual row must include:

1. `beat_id`
2. `visual_id`
3. narration line IDs or phrase IDs
4. target timeline start and end
5. target duration
6. exact visual requirement
7. why this visual is needed for the narration
8. footage search intent
9. acceptable evidence types
10. unacceptable substitutions

Example shape:

```text
| Beat | Visual | VO range | Target | Requirement | Search intent | Reject |
| B003 | B003-V02 | L012 00:41-00:47 | 00:41-00:47 | readable Terrasplit 700% / 7-hit proof | ultimate trace menu or direct Terrasplit combat with damage ticks | generic combat with no ultimate proof |
```

Rules:

1. A beat over 12 seconds normally needs several visual spans.
2. Visual spans must cover the beat's target time range without pretending one clip covers unrelated narration.
3. Menu proof is acceptable only when the narration needs readable menu text.
4. Mechanics narration should usually pair menu proof with combat proof.
5. The artifact must be grounded by searching the existing footage skims while planning, not by inventing ideal footage.

## Pass A Part 2: Refinement and Verification

Revisit `visual_requirements.md`, then ask for every visual how it ties back to the Beat narration as a whole. Is it specific enough to the topic of the beat? Does the visual direction answer what the narration needs?

## Pass A Part 3: Sub-target Line Splitting

Use the word-level timings of `alignment_full.json` to revisit `visual_requirements.md` and add a `Sub-target` column immediately to the right of the `Target` column.

The `Sub-target` column is not just a single copied line range. It is the editorial cut plan inside the visual span.

For every visual, ask what the narration is doing inside the beat:

1. If the visual is one continuous idea, write one clear range.
2. If the visual contains an ordered list, comparison, montage, cause/effect sequence, menu-to-combat sequence, or multiple named items, split it into multiple micro-ranges.
3. Each micro-range must have an editorial label that says what the sub-span should communicate, not just the spoken word.
4. Use `<br>` line breaks between micro-ranges so the table is readable.
5. Keep the micro-ranges tied to word timings, but do not blindly split every word. Group words into meaningful phrase-level edit beats.

Example:

```text
| Beat | Visual | Target | Sub-target | Requirement |
| B009 | B009-V03 | 03:09-03:11 | top priority: Burst 03:09.86-03:10.44<br>second priority: Talent 03:10.44-03:11.12<br>third priority: Skill 03:11.12-03:11.42 | Burst over talent over skill priority |
```

Pass B must later harvest candidates against these micro-ranges when they exist. A row with three sub-target lines should not become one undifferentiated candidate bucket in later passes.

No additional artifact needs to be produced for this step.

## Pass A Part 4: Domain knowledge based refinement

Read the domain knowledge information in docs or ask the user to pass on relevant domain information and use those to refine the `visual_requirements.md` into a new file called `visual_requirements_domain_refined.md`. Most important is information that helps in identification and classification of visual elements within the footage or helps to understand the voice over with higher fidelity. Ground that knowledge you gained against the existing footage and vo. Create first a chapter with "overrides". And once that is completed you integrate it into the main table and replace the old text with the corrected one and then delete the override chapter. Do the replacements one by one or in small batches. Don't try to do the replacements in one go. Do first the replacements and then delete the overrides chapter and not the other way to avoid data loss.

## Pass B: Candidate Harvesting

For every visual from Pass A, search for suitable candidate clip slices. Candidate slices must be source ranges that can fill or support the visual span. Use existing domain knowledge to your advantage to find relevant clips.

The artifact is `candidate_moments.json`.

This is not a list of all clips in the project. It is a mapping:

```text
beat_id -> visual_id -> candidates[]
```

Each candidate must include:

1. `candidate_id`
2. `beat_id`
3. `visual_id`
4. media path
5. skim path
6. source start and end
7. source duration
8. target visual span it could fill
9. evidence from the skim
10. match type: `direct`, `partial`, `context`, or `wrong`
11. risks: readability, duration, mismatch, repeated source, zero-duration skim, missing mode label, etc.

Candidate harvesting should be generous, but scoped. For a visual about Feixiao's E2, candidates should be E2 menu proof, E2-related follow-up/stack combat proof, and close alternatives. It should not include every good-looking clip from unrelated sections.

## Pass C: Candidate Ranking / Evidence Comparison

Rank candidates separately for every visual. The artifact is `ranked_evidence_table.md`.

The table must be grouped by `beat_id` and `visual_id`. A valid ranked table compares candidates for one visual against that visual's requirement. It is not a global count or global sort.

Each visual group must include:

1. visual requirement and target span
2. candidate rank within that visual
3. candidate source range
4. evidence summary
5. what the candidate proves
6. what it does not prove
7. duration fit
8. readability fit
9. continuity/pacing fit
10. final decision: `selected`, `backup`, `reject`, or `manual-review`

Example shape:

```text
### B003-V02 - Terrasplit 700% / 7-hit proof, target 00:41-00:47

| Rank | Candidate | Source | Decision | Evidence | Missing / risk |
| 1 | ultimate_E001 | ultimate.mp4 00:00-00:09 | selected | menu states 700%, weakness ignore, 100% break efficiency | static menu, needs short readable hold |
| 2 | AS_E020 | topazrobinASfullE0S1.mp4 01:01.60-01:06.40 | backup | Terrasplit damage and break proof | does not show the 700% text |
| 3 | random_combat | ... | reject | damage is visible | no Terrasplit or 700% evidence |
```

Hard rule: if the ranked evidence table cannot explain why a selected clip was chosen for that exact visual span, the edit is not ready for timeline assembly.

## Pass D: Timeline Assembly

Build the timeline from Pass C selections. The artifact is `timeline_assembly.md`.

Every row must sequence selected clip slices against original beat and VO timings.

Required columns:

1. beat ID
2. visual ID
3. VO line IDs
4. VO text excerpt
5. timeline in
6. timeline out
7. target duration
8. selected media
9. source in
10. source out
11. source duration
12. ranking reference
13. assembly note
14. continuity note
15. unresolved/manual-review note when applicable

Timeline rows must be ordered by target time. If one visual needs several clips, split it into several timeline rows or explicitly list the ordered subclips with their timeline in/out values.

Do not rely on an exporter to repeat-fill missing duration. If a visual span is underfilled, go back to Pass B or mark the row as manual review.

## Evolution Loop

After Pass D, write `assembly_quality_report.md`, `change_brief.md`, `feedback_log.md`, and `review_notes.md`.

Compare against the previous iteration:

1. long single-clip beat count
2. visuals without candidates
3. visuals without selected candidates
4. selected clips with weak evidence
5. menu-only mechanics spans
6. source-start-zero clustering
7. repeated adjacent assets
8. unresolved/manual-review rows
9. Resolve import/relink/render status, when applicable

If the iteration still has unresolved quality failures, create the next version folder and repeat the same A-D workflow. Do not skip directly to timeline assembly.

## Hard Quality Gates

An iteration is not deliverable while any of these are true:

1. A beat has no Pass A visual breakdown.
2. A visual has no Pass B candidate list.
3. A selected timeline row has no Pass C ranking group.
4. `ranked_evidence_table.md` is a global clip list instead of per-visual comparison.
5. A long beat is represented by one clip without an explicit intentional hold.
6. Mechanics narration is covered only by generic mood footage.
7. Menu proof is used when the text is not readable or not relevant.
8. A visual span is underfilled without going back to candidate search or marking manual review.
9. Resolve readiness is claimed before import, relink or timeline recreation, and basic playback or render verification have succeeded.

## Repo Contracts To Use

When producing machine-readable artifacts, use existing repository concepts rather than inventing parallel schemas:

1. `ShotInstruction.min_clips`, `ShotInstruction.max_clips`, `preferred_assets`, and `required_events`
2. `AssemblySlot.selected_clips`, `selected`, `alternatives`, `unresolved_reason`, `warnings`, `min_clips`, and `max_clips`
3. `AssemblyProposal` for persisted edit decisions
4. `proposal_to_semantic_edit` for semantic edit export
5. `audit_assembly_quality` and `write_quality_report` for deterministic review
6. `write_proposal_director_notes` for reviewable director notes
7. `build_core_edit.py --planner-mode assembly` when the CLI path is appropriate

These repo contracts do not replace manual editorial judgment. They are serialization and review containers for decisions already made through Passes A, B, C, and D.
