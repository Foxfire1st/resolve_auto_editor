# Domain-Term Alignment Mode (Transcript + Canonical Lexicon)

## Why this mode exists
- Gemini vision already surfaces **canonical character names** (for example "Feixiao") inside clip metadata, but spontaneous VO/transcripts often contain phonetic misspellings ("Fay Shaw").
- Downstream orchestration stitches clips based on alignment IDs. If alignment JSON introduces a spelling that never appears in video metadata, the OTIO/FCPXML planner cannot line up dialogue with the correct clip segments.
- A **transcript-first, term-list-driven alignment** guarantees that every span references \\*only\\* words that were actually spoken while still giving the orchestrator a clean, canonical surface form for search/indexing.

## Requirements & Constraints
1. **Transcript is mandatory** – we always align against `alignment_full.transcript.json` (or chunked variants) produced by the existing Whisper/stable-ts pass. No transcript, no alignment.
2. **Schema must remain unchanged** – spouses of the orchestrator expect `aligned[]` spans with `{id, text, t0, t1, words[]}`. The only safe extension is the `source` string ("script" vs "terms").
3. **Canonical spellings come from a shared domain lexicon** so both audio alignment and clip metadata normalization reference the same string.
4. **No speculative text injection** – if a term from the lexicon never appears in the transcript, we simply omit it instead of fabricating timestamps.
5. **Downstream-friendly IDs** – spans need deterministic IDs so deltas or retries do not reshuffle references (e.g., `TERM_FEIXIAO_001`).

## Input Artifacts
| Artifact | Producer | Format |
| --- | --- | --- |
| Transcript tokens | `stable-ts` pipeline (already saved as `*.transcript.json`) | `[ {"text": "Fae", "norm": "fae", "start": 0.6, "end": 0.7, "line_id": null }, ... ]` |
| Domain term list | Manual curation or auto-export from `assets/hsr/characters.json` | YAML / JSON with canonical + aliases |
| (Optional) term boost file | Same as today (`terms_yake.txt`) | plain text, one term per line, piped to ASR for biasing |

### Suggested domain term schema
```yaml
# assets/lexicons/hsr_characters.yaml
- canonical: "Feixiao"
  type: character
  aliases:
    - "Fei Xiao"
    - "Fay Shaw"
    - "Fei-shao"
  min_tokens: 2           # guardrail: need >=2 tokens inside transcript window
  max_gap_seconds: 1.2    # how far apart alias tokens can be split
  metadata:
    element: "Physical"
    path: "Hunt"
```

## Matching Pipeline (transcript-only mode)
1. **Load transcript tokens**: reuse `TranscriptWord` dataclass from `aligner.py`. Treat it as the authoritative timing source.
2. **Lexicon prep**: build a trie of alias tokens (`norm` form) pointing to the canonical entry. This lets us slide through tokens without repeated string joins.
3. **Window scan**:
   - Slide a window over the transcript (default 12 tokens) and attempt to match alias token sequences.
   - Allow configurable gaps (`max_gap_seconds`) to handle ASR splitting names into two syllables.
   - Score matches using coverage (%) and `SequenceMatcher` ratio between alias text and transcript text; require both to exceed thresholds.
4. **Span creation**:
   - When a match fires, emit an `AlignmentSpan` with `text` set to the **canonical** name but keep the original heard text in `words[]`.
   - `id` format: `TERM_<slug>_<occurrence>` so retries remain stable.
   - `source` = `"terms"` so orchestrator can differentiate when helpful while still accepting the payload.
5. **Debounce collisions**:
   - Prevent overlapping spans for the same canonical term unless the gap between mentions exceeds `min_repeat_gap` (default 2.5s).
   - If aliases overlap across different canonicals, keep the higher-confidence score and mark the loser as "skipped".
6. **Output**: identical JSON contract as script mode. `audio_only` remains empty because we only care about matched windows here.

## Orchestrator & Metadata Hygiene
- **Single canonical string**: The same `canonical` field should be propagated into:
  1. Gemini ingest metadata (normalize tags/characters column during ingestion).
  2. Alignment spans (this plan).
  3. Retrieval indices / event metadata entries.
- **Bidirectional mapping**: Store the alias→canonical map next to the lexicon so the orchestrator can normalize any tool input (LLM outputs, user nudges) before searching.
- **Verification hooks**: During clip selection, if a plan references `Feixiao`, the orchestrator can quickly confirm that at least one candidate clip segment contains that canonical name tag. No fuzzy matching is needed at this stage.

## Implementation Breakdown
1. **Shared data classes** (`domain_terms.py`)
   - `DomainTerm`, `DomainAlias`, `TermMatch`.
   - Loader that accepts YAML/JSON/CSV so the same file feeds both ASR boosters and orchestrator lookup tables.
2. **Matcher module** (`match_terms.py`)
   - `match_terms(transcript_tokens, domain_terms, *, thresholds)` → `list[AlignmentSpan]`.
   - Reuse `_normalize_similarity_text` helpers from `aligner.py` to avoid duplicate logic.
3. **CLI updates** (`src/gateway/audio/vo_script_alignment/cli.py`)
   - Add `--mode [script|terms]` (default `script`).
   - Terms mode requires `--transcript-json` and `--domain-terms`.
   - Optional `--aliases-only` flag to output a diagnostics table showing which aliases mapped to which canonical.
4. **Result writer**
   - Both modes call the same `write_alignment(result, out_path)` so orchestrator storage stays identical.
5. **Tests** (`src/gateway/audio/tests/test_alignment_helpers.py` + new file)
   - Fixtures for transcripts that say "Fay Shaw" while lexicon expects "Feixiao".
   - Assert that spans use the canonical `text` but `words[0]['text'] == "Fae"` etc.
   - Edge case: alias absent → no span.
6. **Gemini ingest normalization**
   - After JSON repair and before saving `meta/*.json`, run the same alias→canonical normalization on any entity/tag fields.
   - Store both `raw_tags` and `normalized_tags` if we want traceability.

## Edge Cases & Guardrails
- **Homophones**: e.g., "Firefly" vs "Fire Fly" – require `min_tokens` (>=2) or use `type` to break ties.
- **Back-to-back mentions**: ensure `TERM_FEIXIAO_001` and `_002` are emitted only when the speaker actually repeats the name (enforced via `min_repeat_gap`).
- **Nested aliases**: If "Dan Heng" (2 tokens) contains "Dan" (1 token) for a different entry, bias toward the longer alias; we can compute priority using alias length.
- **ASR drift**: When Whisper outputs "Fei" and "Chiao" we still want to match; allow alias to specify a regex-based fallback or add additional alias entries.

## Rollout Steps
1. **Finish matcher + tests**.
2. **Expose CLI flags & write docs** (this file serves as the spec; reference it from the brief).
3. **Normalize Gemini metadata** using the same lexicon to guarantee clip catalog consistency.
4. **Update orchestrator prompts** to mention "canonical character names only" and to expect `source="terms"` spans during free-speech workflows.
5. **QA**
   - Run on Feixiao sample: verify transcripts mention "Fae Shao" but spans read "Feixiao" and share IDs with metadata tags.
   - Verify plan-builder no longer needs fuzzy fallback between "Fay Shaw" and "Feixiao".

Keeping the transcript as the single source of truth while projecting canonical names onto spans ensures both **sides** of the system (audio alignment and clip metadata) speak the same language, eliminating the accidental reliance on vector similarity to bridge spelling gaps.
