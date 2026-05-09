"""VO ↔ script alignment utilities.

USAGE:
    from pathlib import Path
    from gateway.audio.vo_script_alignment.aligner import align_script_to_audio

    result = align_script_to_audio(Path("script.md"), Path("voice_over.wav"))
    for span in result.aligned:
        print(span.id, span.t0, span.t1, span.text)

The module parses Markdown-ish scripts into line spans, runs (or consumes) a
stable-ts/Whisper transcription with word timestamps, and aligns script lines to
spoken words. Script-only text is ignored, while audio-only passages are
returned as `audio_only` spans so downstream tooling can decide what to do.
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import re
import shutil
import subprocess
import tempfile
import unicodedata
import yake
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

try:  # pragma: no cover - optional dependency
    from wordfreq import zipf_frequency
except Exception:  # pragma: no cover
    zipf_frequency = None

LOGGER = logging.getLogger(__name__)
DEFAULT_MODEL_SIZE = "large-v3"

_UNICODE_PUNCT_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "’": "'",
        "‘": "'",
        "‚": "'",
        "‛": "'",
        "–": "-",
        "—": "-",
        "―": "-",
        "‑": "-",
        "‒": "-",
        "…": "...",
    }
)

_WORD_RE = re.compile(r"[A-Za-z0-9']+%?")


@dataclass(slots=True)
class ScriptLine:
    """Represents a sanitized line from the guide script."""

    id: str
    text: str
    norm_tokens: list[str]


@dataclass(slots=True)
class TranscriptWord:
    """Single word in the VO transcript with timing metadata."""

    text: str
    norm: str
    start: float
    end: float
    line_id: Optional[str] = None


@dataclass(slots=True)
class AlignmentSpan:
    """Output span with start/end timestamps."""

    id: str
    text: str
    t0: float
    t1: float
    source: str = "script"
    words: list[dict] | None = None

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "text": self.text,
            "t0": round(self.t0, 3),
            "t1": round(self.t1, 3),
            "source": self.source,
        }
        if self.words:
            payload["words"] = [
                {
                    "text": entry["text"],
                    "t0": round(entry["t0"], 3),
                    "t1": round(entry["t1"], 3),
                }
                for entry in self.words
            ]
        return payload


@dataclass(slots=True)
class AlignmentResult:
    """Container for aligned script spans, audio-only spans, and skipped lines."""

    aligned: list[AlignmentSpan]
    audio_only: list[AlignmentSpan]
    skipped_script_ids: list[str]

    def to_dict(self) -> dict:
        return {
            "aligned": [span.to_dict() for span in self.aligned],
            "audio_only": [span.to_dict() for span in self.audio_only],
            "skipped_script_ids": self.skipped_script_ids,
        }

    def to_json(self, **json_kwargs) -> str:
        return json.dumps(self.to_dict(), **({"indent": 2} | json_kwargs))


def align_script_to_audio(
    script_path: Path,
    audio_path: Path,
    *,
    transcription: Optional[Sequence[TranscriptWord]] = None,
    model_size: str = DEFAULT_MODEL_SIZE,
    device: str | None = None,
    terms_path: Path | None = None,
    terms_text: str | None = None,
    similarity_threshold: float = 0.72,
    coverage_threshold: float = 0.6,
    search_window: int = 96,
    pad_seconds: float = 0.04,
    max_span_seconds: float | None = 8.0,
) -> AlignmentResult:
    """Align a Markdown script to a voice-over WAV using stable-ts/Whisper.

    Args:
        script_path: Path to the script file (Markdown or plain text).
        audio_path: Path to the voice-over audio.
        transcription: Optional precomputed transcript tokens (used in tests).
        model_size: Whisper/stable-ts model to load if transcription is None.
        device: Optional device string/object for stable-ts (e.g., "cuda", "cpu", "dml").
        terms_path: Optional newline-delimited file with boosted domain terms.
        similarity_threshold: Minimum SequenceMatcher ratio for accepting match.
        coverage_threshold: Minimum token coverage for accepting match.
        search_window: Max number of transcript tokens to scan ahead.
        pad_seconds: Extra padding applied to t0/t1 to include breaths.

    Returns:
        AlignmentResult containing aligned spans, audio-only spans, and script IDs
        that were skipped because no speech matched them.
    """

    script_lines = _load_script_lines(script_path)
    if not script_lines:
        raise ValueError(f"No usable lines parsed from {script_path}")

    tokens = list(transcription) if transcription is not None else _transcribe_with_wav(
        audio_path=audio_path,
        model_size=model_size,
        device=device,
        terms_path=terms_path,
        terms_text=terms_text,
    )
    if not tokens:
        raise ValueError("Transcription produced no tokens; cannot align.")

    tokens = sorted(tokens, key=lambda t: (t.start, t.end))

    return _align_loaded(
        script_lines=script_lines,
        tokens=tokens,
        similarity_threshold=similarity_threshold,
        coverage_threshold=coverage_threshold,
        search_window=search_window,
        pad_seconds=pad_seconds,
        max_span_seconds=max_span_seconds,
    )


def align_script_to_audio_chunked(
    script_path: Path,
    audio_path: Path,
    *,
    chunk_seconds: float = 60.0,
    overlap_seconds: float = 1.0,
    model_size: str = DEFAULT_MODEL_SIZE,
    device: str | None = None,
    terms_path: Path | None = None,
    terms_text: str | None = None,
    similarity_threshold: float = 0.72,
    coverage_threshold: float = 0.6,
    search_window: int = 96,
    pad_seconds: float = 0.04,
    max_span_seconds: float | None = 8.0,
) -> AlignmentResult:
    """Align a long VO by slicing it into overlapping chunks to speed up processing."""

    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be > 0.")
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds must be >= 0 and smaller than chunk_seconds.")

    script_lines = _load_script_lines(script_path)
    if not script_lines:
        raise ValueError(f"No usable lines parsed from {script_path}")

    with _ensure_wav(audio_path) as wav_path:
        duration = _get_audio_duration(wav_path)
        chunks = list(_iter_chunks(duration, chunk_seconds, overlap_seconds))
        if not chunks:
            raise ValueError("No chunks generated; audio may be empty.")

        tokens = _transcribe_chunks_in_parallel(
            audio_path=wav_path,
            chunks=chunks,
            model_size=model_size,
            device=device,
            terms_path=terms_path,
            terms_text=terms_text,
        )
        tokens = sorted(tokens, key=lambda t: (t.start, t.end))

    result = _align_loaded(
        script_lines=script_lines,
        tokens=tokens,
        similarity_threshold=similarity_threshold,
        coverage_threshold=coverage_threshold,
        search_window=search_window,
        pad_seconds=pad_seconds,
        max_span_seconds=max_span_seconds,
    )

    return result


def _align_loaded(
    *,
    script_lines: Sequence[ScriptLine],
    tokens: list[TranscriptWord],
    similarity_threshold: float,
    coverage_threshold: float,
    search_window: int,
    pad_seconds: float,
    max_span_seconds: float | None,
) -> AlignmentResult:
    # Build a canonical lexicon from the script to normalize misheard tokens.
    canonical_terms = {token for line in script_lines for token in line.norm_tokens}
    normalized_tokens = _normalize_transcript_tokens(tokens, canonical_terms)

    aligned: list[AlignmentSpan] = []
    skipped: list[str] = []
    cursor = 0
    for line in script_lines:
        if not line.norm_tokens:
            LOGGER.debug("Skipping empty-normalized line %s", line.id)
            skipped.append(line.id)
            continue

        match = _find_match(
            line=line,
            tokens=normalized_tokens,
            start_idx=max(cursor - 8, 0),
            similarity_threshold=similarity_threshold,
            coverage_threshold=coverage_threshold,
            search_window=search_window,
            max_span_seconds=max_span_seconds,
        )
        if not match:
            LOGGER.info("No audio found for script line %s: %s", line.id, line.text)
            skipped.append(line.id)
            continue

        LOGGER.debug(
            "Aligned %s tokens %s-%s with score %.3f",
            line.id,
            match.start,
            match.end,
            match.score,
        )
        for idx in range(match.start, match.end):
            tokens[idx].line_id = line.id
            normalized_tokens[idx].line_id = line.id
        chunk_raw = tokens[match.start : match.end]
        chunk_norm = normalized_tokens[match.start : match.end]
        audio_text = _normalize_similarity_text(_join_chunk_text(chunk_raw))
        script_text_norm = _normalize_similarity_text(line.text)
        similarity_score = (
            SequenceMatcher(None, script_text_norm, audio_text).ratio()
            if audio_text and script_text_norm
            else 0.0
        )
        effective_similarity = max(similarity_threshold, 0.85)
        if similarity_score < effective_similarity:
            for idx in range(match.start, match.end):
                tokens[idx].line_id = None
                normalized_tokens[idx].line_id = None
            LOGGER.info(
                "Dropping script line %s due to low similarity %.3f", line.id, similarity_score
            )
            skipped.append(line.id)
            continue

        t0 = max(chunk_raw[0].start - pad_seconds, 0.0)
        t1 = chunk_raw[-1].end + pad_seconds
        aligned.append(
            AlignmentSpan(
                id=line.id,
                text=line.text,
                t0=t0,
                t1=t1,
                source="script",
                words=_line_word_spans(line, chunk_raw, chunk_norm),
            )
        )
        cursor = match.end

    audio_only = _collect_audio_only_spans(tokens, pad_seconds)
    if audio_only:
        aligned.extend(audio_only)
        aligned.sort(key=lambda span: span.t0)
    return AlignmentResult(aligned=aligned, audio_only=[], skipped_script_ids=skipped)


def _load_script_lines(script_path: Path) -> list[ScriptLine]:
    text = Path(script_path).read_text(encoding="utf-8")
    lines: list[ScriptLine] = []
    counter = 1
    for raw_line in text.splitlines():
        cleaned = _clean_script_line(raw_line)
        if not cleaned:
            continue
        if _is_header_line(raw_line, cleaned):
            continue
        for sentence in _split_sentences(cleaned):
            norm_tokens = _normalize_tokens(sentence)
            if not norm_tokens:
                continue
            line_id = f"L{counter:03d}"
            counter += 1
            lines.append(ScriptLine(id=line_id, text=sentence, norm_tokens=norm_tokens))
    return lines


def _clean_script_line(line: str) -> str:
    normalized = _normalize_unicode_punctuation(line)
    stripped = normalized.strip()
    if not stripped:
        return ""
    stripped = re.sub(r"^[\-\*\d\.\)\s>#+]+", "", stripped)
    stripped = stripped.strip()
    return _strip_markdown_markup(stripped)


_MARKDOWN_SYMBOL_RE = re.compile(r"[*_`~]+")


def _strip_markdown_markup(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^[_*`~]+|[_*`~]+$", "", text)
    text = text.replace("\\!", "!").replace("\\?", "?").replace("\\.", ".")
    return text


def _normalize_similarity_text(text: str) -> str:
    text = _strip_markdown_markup(text)
    text = re.sub(r"[^A-Za-z0-9'\s]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_header_line(raw_line: str, cleaned: str) -> bool:
    stripped = raw_line.lstrip()
    if stripped.startswith("#"):
        return True
    if not cleaned:
        return True
    plain = cleaned.strip().strip("*_ ")
    if not plain:
        return True
    alpha = re.sub(r"[^A-Za-z0-9]", "", plain)
    if not alpha:
        return False
    # Treat all-caps short lines without sentence punctuation as headers (e.g., "ULTIMATE")
    if (
        plain.upper() == plain
        and len(plain.split()) <= 5
        and not any(ch in plain for ch in ".?!")
    ):
        return True
    return False


def _normalize_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text.lower()):
        token = match.group(0)
        if token.endswith("%"):
            token = token[:-1]
        tokens.append(token)
    return tokens


def _normalize_unicode_punctuation(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return normalized.translate(_UNICODE_PUNCT_TRANSLATION)


def _normalize_transcript_tokens(
    tokens: Sequence[TranscriptWord],
    canonical_terms: set[str],
    *,
    min_ratio: float = 0.8,
) -> list[TranscriptWord]:
    """Fuzzy-normalize transcript tokens against canonical terms while preserving timestamps."""
    if not canonical_terms:
        return list(tokens)
    normalized: list[TranscriptWord] = []
    for tok in tokens:
        norm = tok.norm.lower() if tok.norm else ""
        best = norm
        best_ratio = 0.0
        for cand in canonical_terms:
            ratio = SequenceMatcher(None, norm, cand).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = cand
            if best_ratio >= 0.95:
                break
        if best_ratio >= min_ratio:
            normalized.append(
                TranscriptWord(
                    text=tok.text,
                    norm=best,
                    start=tok.start,
                    end=tok.end,
                    line_id=tok.line_id,
                )
            )
        else:
            normalized.append(tok)
    return normalized


def _split_sentences(text: str) -> list[str]:
    """Split a cleaned line into sentences while keeping punctuation at the end."""
    # Basic splitter on ., ?, ! followed by whitespace; keeps the delimiter.
    parts = re.split(r"(?<=[.!?])\s+", text)
    sentences: list[str] = []
    for part in parts:
        trimmed = part.strip()
        if trimmed:
            sentences.append(trimmed)
    return sentences


def extract_terms_from_script(script_path: Path, *, max_terms: int = 256) -> tuple[str, list[str]]:
    """Derive domain terms from a script using YAKE keywords only."""

    text = script_path.read_text(encoding="utf-8")
    terms = _extract_yake_terms(text, max_terms=max_terms)
    return "\n".join(terms), terms


@dataclass(slots=True)
class _MatchWindow:
    start: int
    end: int
    score: float

def _evaluate_window(
    line_norm: str, line_tokens: Sequence[str], chunk: Sequence[tuple[int, TranscriptWord]]
) -> tuple[float, float, float, float]:
    if not chunk:
        return 0.0, 0.0, 0.0, 0.0
    chunk_norm = " ".join(tok.norm for _, tok in chunk)
    ratio = SequenceMatcher(None, line_norm, chunk_norm).ratio()
    coverage = _token_coverage(line_tokens, [tok for _, tok in chunk])
    length_ratio = _token_length_similarity(len(line_tokens), len(chunk))
    score = ((ratio + coverage) / 2.0) * math.pow(length_ratio, 1.2)
    return ratio, coverage, length_ratio, score


def _prune_match_window(
    line_norm: str,
    line_tokens: Sequence[str],
    chunk: list[tuple[int, TranscriptWord]],
    *,
    min_ratio: float,
    min_coverage: float,
    line_text: str,
) -> list[tuple[int, TranscriptWord]]:
    expected = len(line_tokens)
    if not chunk or expected <= 0:
        return chunk
    _, _, _, best_score = _evaluate_window(line_norm, line_tokens, chunk)
    best_chunk = chunk
    changed = True
    while changed and len(best_chunk) > expected:
        changed = False
        # Try trimming the end of the chunk first.
        trimmed_end = best_chunk[:-1]
        if trimmed_end:
            ratio, coverage, _, score = _evaluate_window(line_norm, line_tokens, trimmed_end)
            if ratio >= min_ratio and coverage >= min_coverage and score >= best_score * 0.98:
                best_chunk = trimmed_end
                best_score = score
                changed = True
                continue
        # Then try trimming the beginning if still too long.
        trimmed_start = best_chunk[1:]
        if trimmed_start:
            ratio, coverage, _, score = _evaluate_window(line_norm, line_tokens, trimmed_start)
            if ratio >= min_ratio and coverage >= min_coverage and score >= best_score * 0.98:
                best_chunk = trimmed_start
                best_score = score
                changed = True
    trimmed = _trim_after_terminal_punctuation(line_text, best_chunk)
    return _trim_after_token_alignment(line_tokens, trimmed)


def _trim_after_terminal_punctuation(
    line_text: str, chunk: list[tuple[int, TranscriptWord]]
) -> list[tuple[int, TranscriptWord]]:
    stripped = line_text.strip()
    if not stripped or stripped[-1] not in ".!?":
        return chunk
    last_punct_idx: int | None = None
    for idx, (_, tok) in enumerate(chunk):
        if tok.text.endswith((".", "!", "?")):
            last_punct_idx = idx
    if last_punct_idx is not None and last_punct_idx < len(chunk) - 1:
        return chunk[: last_punct_idx + 1]
    return chunk


def _trim_after_token_alignment(
    line_tokens: Sequence[str], chunk: list[tuple[int, TranscriptWord]]
) -> list[tuple[int, TranscriptWord]]:
    if not line_tokens or not chunk:
        return chunk
    chunk_norms = [tok.norm for _, tok in chunk]
    matcher = SequenceMatcher(None, list(line_tokens), chunk_norms)
    end_idx: int | None = None
    for a, b, size in matcher.get_matching_blocks():
        if size == 0:
            continue
        if a + size == len(line_tokens):
            end_idx = b + size
    if end_idx is not None and end_idx < len(chunk):
        return chunk[:end_idx]
    return chunk


def _find_match(
    line: ScriptLine,
    tokens: Sequence[TranscriptWord],
    *,
    start_idx: int,
    similarity_threshold: float,
    coverage_threshold: float,
    search_window: int,
    max_span_seconds: float | None,
) -> Optional[_MatchWindow]:
    if not line.norm_tokens:
        return None
    line_norm = " ".join(line.norm_tokens)
    best: Optional[_MatchWindow] = None
    max_len = len(tokens)
    search_limit = min(max_len, start_idx + search_window)

    for start in range(start_idx, search_limit):
        if tokens[start].line_id is not None:
            continue

        min_window = max(1, len(line.norm_tokens) // 2)
        overhang = max(4, int(len(line.norm_tokens) * 0.4))
        max_window = min(max_len, start + len(line.norm_tokens) + overhang)

        for end in range(start + min_window, max_window + 1):
            chunk = [
                (idx, tok)
                for idx, tok in ((i, tokens[i]) for i in range(start, end))
                if tok.line_id is None and tok.norm
            ]
            if not chunk:
                continue
            if max_span_seconds is not None:
                span_duration = chunk[-1][1].end - chunk[0][1].start
                if span_duration > max_span_seconds:
                    continue
            chunk = _prune_match_window(
                line_norm=line_norm,
                line_tokens=line.norm_tokens,
                chunk=chunk,
                min_ratio=similarity_threshold,
                min_coverage=coverage_threshold,
                line_text=line.text,
            )
            if not chunk:
                continue
            ratio, coverage, _, score = _evaluate_window(line_norm, line.norm_tokens, chunk)
            if ratio >= similarity_threshold and coverage >= coverage_threshold:
                match_start = chunk[0][0]
                match_end = chunk[-1][0] + 1
                if best is None or score > best.score or (score == best.score and match_start < best.start):
                    best = _MatchWindow(start=match_start, end=match_end, score=score)
    return best


def _token_coverage(line_tokens: Sequence[str], chunk: Sequence[TranscriptWord]) -> float:
    if not line_tokens:
        return 0.0
    line_counts = Counter(line_tokens)
    chunk_counts = Counter(tok.norm for tok in chunk)
    matched = sum(min(line_counts[token], chunk_counts[token]) for token in line_counts)
    return matched / len(line_tokens)


def _token_length_similarity(expected: int, observed: int) -> float:
    if expected <= 0 or observed <= 0:
        return 0.0
    smaller = min(expected, observed)
    larger = max(expected, observed)
    return smaller / larger


def _collect_audio_only_spans(tokens: Sequence[TranscriptWord], pad: float) -> list[AlignmentSpan]:
    audio_only: list[AlignmentSpan] = []
    chunk: list[TranscriptWord] = []
    gap_threshold = 1.2
    counter = 1

    def flush():
        nonlocal chunk, counter, audio_only
        if len(chunk) < 2:
            chunk.clear()
            return
        text = _join_chunk_text(chunk)
        if not text.strip():
            chunk.clear()
            return
        span = AlignmentSpan(
            id=f"A{counter:03d}",
            text=text.strip(),
            t0=max(chunk[0].start - pad, 0.0),
            t1=chunk[-1].end + pad,
            source="audio",
            words=_tokens_to_word_spans(chunk),
        )
        counter += 1
        audio_only.append(span)
        chunk.clear()

    prev_end = None
    for token in tokens:
        if token.line_id is not None or not token.norm:
            flush()
            prev_end = token.end
            continue
        if prev_end is not None and (token.start - prev_end) > gap_threshold:
            flush()
        chunk.append(token)
        prev_end = token.end
    flush()
    return audio_only


def _join_chunk_text(chunk: Sequence[TranscriptWord]) -> str:
    words = [tok.text.strip() for tok in chunk if tok.text.strip()]
    return " ".join(words)


def _tokens_to_word_spans(chunk: Sequence[TranscriptWord]) -> list[dict[str, float | str]]:
    return [
        {
            "text": tok.text,
            "t0": tok.start,
            "t1": tok.end,
        }
        for tok in chunk
        if tok.text.strip()
    ]


def _line_word_spans(
    line: ScriptLine,
    chunk_raw: Sequence[TranscriptWord],
    chunk_norm: Sequence[TranscriptWord],
) -> list[dict[str, float | str]]:
    matches = list(_WORD_RE.finditer(line.text))
    if not matches:
        return _tokens_to_word_spans(chunk_raw)
    if not chunk_raw:
        return [{"text": match.group(0), "t0": 0.0, "t1": 0.0} for match in matches]

    script_words = [match.group(0) for match in matches]
    script_norms = [word.lower().rstrip("%") for word in script_words]
    chunk_norms: list[str] = []
    for tok in chunk_norm:
        norm = tok.norm
        if not norm:
            normalized = _normalize_tokens(tok.text)
            norm = normalized[0] if normalized else tok.text.lower()
        chunk_norms.append(norm)

    matcher = SequenceMatcher(None, script_norms, chunk_norms, autojunk=False)
    spans: list[dict[str, float | str]] = []
    last_end = chunk_raw[0].start

    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == "equal":
            for offset in range(a1 - a0):
                token = chunk_raw[b0 + offset]
                word = script_words[a0 + offset]
                spans.append({"text": word, "t0": token.start, "t1": token.end})
                last_end = token.end
        elif op in {"replace", "delete"}:
            spans.extend(
                _assign_script_words_to_chunk(
                    script_words[a0:a1],
                    chunk_raw,
                    b0,
                    b1,
                    last_end,
                )
            )
            if b1 > b0:
                last_end = chunk_raw[b1 - 1].end
        elif op == "insert":
            if b1 > b0:
                last_end = chunk_raw[b1 - 1].end

    if spans:
        return spans
    fallback_time = chunk_raw[0].start if chunk_raw else 0.0
    return _assign_script_words_to_chunk(
        script_words,
        chunk_raw,
        0,
        len(chunk_raw),
        fallback_time,
    )


def _assign_script_words_to_chunk(
    script_words: Sequence[str],
    chunk: Sequence[TranscriptWord],
    chunk_start: int,
    chunk_end: int,
    fallback_time: float,
) -> list[dict[str, float | str]]:
    if not script_words:
        return []
    if not chunk:
        return [{"text": word, "t0": fallback_time, "t1": fallback_time} for word in script_words]
    assigned: list[dict[str, float | str]] = []
    chunk_len = len(chunk)
    chunk_start = max(0, min(chunk_start, chunk_len - 1))
    chunk_end = max(chunk_start + 1, min(chunk_end, chunk_len))
    chunk_slice = chunk[chunk_start:chunk_end]

    token_count = len(chunk_slice)
    parts = len(script_words)
    for idx, word in enumerate(script_words):
        if parts == 1:
            subset = chunk_slice
        else:
            seg_start = int(idx * token_count / parts)
            seg_end = int((idx + 1) * token_count / parts)
            if seg_end <= seg_start:
                seg_end = min(token_count, seg_start + 1)
            subset = chunk_slice[seg_start:seg_end]
        if not subset:
            subset = chunk_slice[-1:]
        t0 = subset[0].start
        t1 = subset[-1].end
        assigned.append({"text": word, "t0": t0, "t1": t1})

    return assigned




def _transcribe_audio(
    audio_path: Path,
    *,
    model_size: str,
    device: str | None,
    terms_path: Path | None,
    terms_text: str | None,
    model: Any | None = None,
) -> List[TranscriptWord]:
    own_model = False
    if model is None:
        model = _load_stable_model(model_size=model_size, device=device)
        own_model = True

    initial_prompt = _prepare_terms_payload(
        terms_text=terms_text,
        terms_path=terms_path,
    )

    result = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        initial_prompt=initial_prompt,
    )
    if own_model:
        del model

    segments = None
    if isinstance(result, dict):
        segments = result.get("segments")
    else:
        segments = getattr(result, "segments", None)
    if segments is None:
        raise RuntimeError("stable-ts transcription result missing 'segments'.")

    words: list[TranscriptWord] = []
    for segment in segments:
        seg_words = segment.get("words", []) if isinstance(segment, dict) else getattr(segment, "words", None)
        if not seg_words:
            continue
        seg_start = segment.get("start", 0.0) if isinstance(segment, dict) else getattr(segment, "start", 0.0)
        seg_end = segment.get("end", seg_start) if isinstance(segment, dict) else getattr(segment, "end", seg_start)
        for word in seg_words:
            if isinstance(word, dict):
                raw = str(word.get("word", "")).strip()
                start = float(word.get("start", seg_start))
                end = float(word.get("end", seg_end if seg_end is not None else seg_start))
            else:
                raw = str(getattr(word, "word", "")).strip()
                start = float(getattr(word, "start", seg_start))
                end = float(getattr(word, "end", seg_end if seg_end is not None else start))
            if not raw:
                continue
            if math.isnan(start) or math.isnan(end):
                continue
            norm = _normalize_tokens(raw)
            words.append(
                TranscriptWord(
                    text=raw,
                    norm=norm[0] if norm else "",
                    start=start,
                    end=end,
                )
            )
    return words


def _transcribe_with_wav(
    *,
    audio_path: Path,
    model_size: str,
    device: str | None,
    terms_path: Path | None,
    terms_text: str | None,
) -> List[TranscriptWord]:
    with _ensure_wav(audio_path) as wav_path:
        return _transcribe_audio(
            audio_path=wav_path,
            model_size=model_size,
            device=device,
            terms_path=terms_path,
            terms_text=terms_text,
        )


def transcribe_audio(
    audio_path: Path,
    *,
    model_size: str = DEFAULT_MODEL_SIZE,
    device: str | None = None,
    terms_path: Path | None = None,
    terms_text: str | None = None,
) -> List[TranscriptWord]:
    """Public helper to transcribe audio into tokens with timestamps."""
    return _transcribe_with_wav(
        audio_path=audio_path,
        model_size=model_size,
        device=device,
        terms_path=terms_path,
        terms_text=terms_text,
    )


def transcript_tokens_to_json(tokens: Sequence[TranscriptWord]) -> list[dict[str, float | str]]:
    return [
        {
            "text": token.text,
            "norm": token.norm,
            "start": token.start,
            "end": token.end,
        }
        for token in tokens
    ]


def write_transcript(tokens: Sequence[TranscriptWord], path: Path) -> None:
    payload = transcript_tokens_to_json(tokens)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_transcript(path: Path) -> list[TranscriptWord]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tokens: list[TranscriptWord] = []
    for entry in data:
        text = str(entry.get("text", ""))
        start = float(entry.get("start", 0.0))
        end = float(entry.get("end", start))
        norm = str(entry.get("norm", ""))
        if not norm:
            normalized = _normalize_tokens(text)
            norm = normalized[0] if normalized else ""
        tokens.append(TranscriptWord(text=text, norm=norm, start=start, end=end))
    return tokens


def _load_stable_model(*, model_size: str, device: str | None):
    try:
        import stable_whisper
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "stable-ts is required for transcription. Install with "
            "`pip install auto-editor[alignment]`."
        ) from exc

    resolved_device = _resolve_device(device)
    LOGGER.info("Loading stable-ts model '%s' on device '%s'", model_size, resolved_device or "auto")
    return stable_whisper.load_model(model_size, device=resolved_device)


def _get_audio_duration(audio_path: Path) -> float:
    try:
        import torchaudio
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("torchaudio is required for chunked alignment.") from exc

    info = torchaudio.info(str(audio_path))
    if not info.sample_rate or not info.num_frames:
        raise ValueError(f"Could not read metadata for {audio_path}")
    return info.num_frames / info.sample_rate


def _iter_chunks(duration: float, chunk_seconds: float, overlap_seconds: float) -> Iterable[tuple[float, float]]:
    start = 0.0
    while start < duration:
        end = min(duration, start + chunk_seconds)
        yield start, end
        if end >= duration:
            break
        start = max(0.0, end - overlap_seconds)


def _extract_audio_chunk(source: Path, start: float, duration: float, target: Path) -> None:
    import torchaudio

    info = torchaudio.info(str(source))
    sample_rate = info.sample_rate
    frame_offset = max(0, int(start * sample_rate))
    num_frames = max(1, int(duration * sample_rate))
    waveform, sr = torchaudio.load(str(source), frame_offset=frame_offset, num_frames=num_frames)
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    if sr != 44100:
        waveform = torchaudio.functional.resample(waveform, sr, 44100)
        sr = 44100
    torchaudio.save(str(target), waveform, sr)


def _transcribe_chunks_in_parallel(
    *,
    audio_path: Path,
    chunks: list[tuple[float, float]],
    model_size: str,
    device: str | None,
    terms_path: Path | None,
    terms_text: str | None,
) -> list[TranscriptWord]:
    if not chunks:
        return []
    max_workers = min(len(chunks), multiprocessing.cpu_count() or 1)
    if max_workers <= 0:
        max_workers = 1

    merged: list[TranscriptWord] = []
    with tempfile.TemporaryDirectory(prefix="vo_chunks_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        jobs: list[tuple[str, float]] = []
        for idx, (start, end) in enumerate(chunks):
            chunk_duration = end - start
            chunk_path = tmpdir_path / f"chunk_{idx:04d}.wav"
            _extract_audio_chunk(audio_path, start, chunk_duration, chunk_path)
            jobs.append((str(chunk_path), start))

        terms_str = str(terms_path) if terms_path else None
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    _transcribe_chunk_worker,
                    chunk_path,
                    start,
                    model_size,
                    device,
                    terms_str,
                    terms_text,
                )
                for chunk_path, start in jobs
            ]
            for future in as_completed(futures):
                merged.extend(future.result())
    return merged


_WORKER_MODELS: dict[tuple[str, str | None], Any] = {}


def _transcribe_chunk_worker(
    chunk_path: str,
    start_offset: float,
    model_size: str,
    device: str | None,
    terms_path: str | None,
    terms_text: str | None,
) -> list[TranscriptWord]:
    model = _get_worker_model(model_size, device)
    terms = Path(terms_path) if terms_path else None
    tokens = _transcribe_audio(
        audio_path=Path(chunk_path),
        model_size=model_size,
        device=device,
        terms_path=terms,
        terms_text=terms_text,
        model=model,
    )
    for token in tokens:
        token.start += start_offset
        token.end += start_offset
    return tokens


def _get_worker_model(model_size: str, device: str | None):
    key = (model_size, device or "auto")
    model = _WORKER_MODELS.get(key)
    if model is None:
        model = _load_stable_model(model_size=model_size, device=device)
        _WORKER_MODELS[key] = model
    return model


def _resolve_device(device: str | Any | None) -> str | Any | None:
    """Normalize requested device string and ensure optional backends are loaded."""

    if device is None:
        return None
    if isinstance(device, str):
        normalized = device.strip().lower()
        if not normalized or normalized == "auto":
            return None
        if normalized in {"dml", "directml"}:
            return _directml_device()
        if "privateuseone" in normalized:
            _ensure_directml_backend()
            return device
    return device


def _directml_device():
    backend = _ensure_directml_backend()
    return backend.device()


def _ensure_directml_backend():
    try:
        import torch_directml  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "DirectML device requested but torch-directml is not installed. "
            "Install it with `pip install torch-directml`."
        ) from exc
    return torch_directml


def _prepare_terms_payload(
    *, terms_text: str | None, terms_path: Path | None, prompt_limit: int = 128
) -> str | None:
    """Build the transcription prompt from the provided terms text/file."""

    raw: str | None = None
    if terms_text and terms_text.strip():
        raw = terms_text
    elif terms_path and terms_path.exists():
        raw = terms_path.read_text(encoding="utf-8")

    if not raw:
        return None

    phrases = _filter_domain_phrases(raw)
    if not phrases:
        return None

    return _format_keywords_prompt(phrases, limit=prompt_limit)


def _format_keywords_prompt(terms: Sequence[str], *, limit: int = 128) -> str | None:
    trimmed = [term.strip() for term in terms if term.strip()]
    if not trimmed:
        return None
    subset = trimmed[:limit]
    return "Keywords: " + ". ".join(subset) + "."


def _filter_domain_phrases(raw: str, max_phrases: int = 256) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\r\n]+", raw):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        tokens = _normalize_tokens(cleaned)
        if not tokens:
            continue
        if not _is_domainish_phrase(tokens):
            continue
        normalized = " ".join(tokens)
        if normalized in seen:
            continue
        seen.add(normalized)
        phrases.append(cleaned)
        if len(phrases) >= max_phrases:
            break
    return phrases


__all__ = [
    "AlignmentResult",
    "AlignmentSpan",
    "TranscriptWord",
    "align_script_to_audio",
    "align_script_to_audio_chunked",
    "transcribe_audio",
    "extract_terms_from_script",
    "transcript_tokens_to_json",
    "write_transcript",
    "load_transcript",
]


@contextmanager
def _ensure_wav(audio_path: Path):
    """Convert non-WAV sources to mono 44.1k WAV for stable timestamps."""
    if audio_path.suffix.lower() in {".wav", ".wave"}:
        yield audio_path
        return

    tmpdir = Path(tempfile.mkdtemp(prefix="vo_wav_"))
    target = tmpdir / "converted.wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "44100",
        str(target),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except Exception as exc:  # pragma: no cover
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"ffmpeg conversion failed for {audio_path}: {exc}") from exc

    try:
        yield target
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _extract_yake_terms(text: str, max_terms: int = 256, *, max_ngram: int = 3) -> list[str]:
    """Use YAKE (Yet Another Keyword Extractor) to pull salient domain terms."""

    if yake is None or not text.strip():  # pragma: no cover - optional dependency
        return []

    extractor = yake.KeywordExtractor(
        lan="en",
        n=max_ngram,
        dedupLim=0.9,
        dedupFunc="seqm",
        windowsSize=1,
        top=max(50, max_terms * 3),
    )

    candidates = extractor.extract_keywords(text)
    if not candidates:
        return []

    terms: list[str] = []
    seen: set[str] = set()
    for keyword, score in sorted(candidates, key=lambda item: item[1]):
        normalized_tokens = _normalize_tokens(keyword)
        if not normalized_tokens:
            continue
        normalized = " ".join(normalized_tokens)
        if normalized in seen:
            continue

        tokens = normalized_tokens
        if not _is_domainish_phrase(tokens):
            continue
        if any(char.isdigit() for char in normalized):
            continue

        seen.add(normalized)
        cleaned = keyword.strip()
        if not cleaned:
            continue
        terms.append(cleaned)
        if len(terms) >= max_terms:
            break

    return terms

def _is_high_frequency_token(token: str, *, threshold: float = 3.8) -> bool:
    if not token.isalpha():
        return False
    if zipf_frequency is None:
        return len(token) <= 3
    return zipf_frequency(token, "en") >= threshold


def _is_domainish_phrase(tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    return any(not _is_high_frequency_token(token) and len(token) > 3 for token in tokens)


def _match_original_token(phrase: str, token: str) -> str | None:
    pattern = re.compile(rf"\b{re.escape(token)}\b", flags=re.IGNORECASE)
    match = pattern.search(phrase)
    if match:
        return match.group(0)
    return None

