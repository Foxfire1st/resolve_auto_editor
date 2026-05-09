"""Tests for the VO/script alignment pipeline."""

from pathlib import Path

from gateway.audio.vo_script_alignment.aligner import (
    AlignmentResult,
    TranscriptWord,
    align_script_to_audio,
    extract_terms_from_script,
    load_transcript,
    write_transcript,
)


def test_align_script_to_audio_with_audio_only_chunk(tmp_path):
    script = tmp_path / "script.md"
    script.write_text(
        """# Intro
Raiden rotation basics
Burst uptime tips
""",
        encoding="utf-8",
    )

    tokens = [
        TranscriptWord("Raiden", "raiden", 0.0, 0.3),
        TranscriptWord("rotation", "rotation", 0.3, 0.6),
        TranscriptWord("basics", "basics", 0.6, 1.0),
        TranscriptWord("burst", "burst", 1.5, 1.9),
        TranscriptWord("uptime", "uptime", 1.9, 2.3),
        TranscriptWord("tips", "tips", 2.3, 2.7),
        TranscriptWord("extra", "extra", 4.0, 4.3),
        TranscriptWord("info", "info", 4.3, 4.6),
        TranscriptWord("segment", "segment", 4.6, 5.0),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("sample.wav"),
        transcription=tokens,
        similarity_threshold=0.6,
    )

    assert isinstance(result, AlignmentResult)
    assert [span.id for span in result.aligned] == ["L001", "L002", "A001"]
    assert result.skipped_script_ids == []
    assert not result.audio_only
    assert result.aligned[-1].source == "audio"


def test_low_similarity_script_line_is_dropped(tmp_path):
    script = tmp_path / "script.md"
    script.write_text(
        """Welcome back to the show.
Call to action for the giveaway.
""",
        encoding="utf-8",
    )

    tokens = [
        TranscriptWord("Welcome", "welcome", 0.0, 0.5),
        TranscriptWord("back", "back", 0.5, 1.0),
        TranscriptWord("to", "to", 1.0, 1.5),
        TranscriptWord("the", "the", 1.5, 2.0),
        TranscriptWord("show", "show", 2.0, 2.5),
        TranscriptWord("Freestyle", "freestyle", 4.0, 4.5),
        TranscriptWord("hype", "hype", 4.5, 5.0),
        TranscriptWord("segment", "segment", 5.0, 5.5),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("sample.wav"),
        transcription=tokens,
        similarity_threshold=0.7,
    )

    assert [span.id for span in result.aligned] == ["L001", "A001"]
    assert result.aligned[-1].source == "audio"
    assert result.skipped_script_ids == ["L002"]

def test_align_handles_unmatched_script_lines(tmp_path):
    script = tmp_path / "guide.md"
    script.write_text(
        """- Opening joke
- Feature explanation
- Call to action
""",
        encoding="utf-8",
    )

    tokens = [
        TranscriptWord("feature", "feature", 0.0, 0.3),
        TranscriptWord("explanation", "explanation", 0.3, 0.7),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("guide.wav"),
        transcription=tokens,
        similarity_threshold=0.6,
    )

    assert [span.id for span in result.aligned] == ["L002"]
    assert result.skipped_script_ids == ["L001", "L003"]
    assert not result.audio_only


def test_extract_terms_from_script_prefers_domain_words(tmp_path):
    script = tmp_path / "fei.md"
    script.write_text(
        """
# Feixiao quickstart

Feixiao's Storm Pulse turret pairs with Acheron's Verdant Blossom to shred elites.
This guide covers Feixiao rotations, Acheron support windows, and Hunt resonance setups.
Ignore the filler words and focus on Feixiao, Acheron, Verdant Blossom, and Hunt resonance.
""",
        encoding="utf-8",
    )

    _, terms = extract_terms_from_script(script, max_terms=16)

    lowered = [term.lower() for term in terms]
    assert any("feixiao" in term for term in lowered)
    assert any("acheron" in term for term in lowered)
    assert "the" not in lowered
    assert "and" not in lowered


def test_alignment_repairs_misheard_tokens_via_elimination(tmp_path):
    script = tmp_path / "fei.md"
    script.write_text("Feixiao demolishes bosses.", encoding="utf-8")

    tokens = [
        TranscriptWord("Feishal", "feishal", 0.0, 0.4),
        TranscriptWord("demolishes", "demolishes", 0.4, 0.9),
        TranscriptWord("bosses", "bosses", 0.9, 1.3),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("sample.wav"),
        transcription=tokens,
        similarity_threshold=0.5,
    )

    span = result.aligned[0]
    assert span.words[0]["text"] == "Feixiao"


def test_markdown_headers_are_skipped(tmp_path):
    script = tmp_path / "headers.md"
    script.write_text(
        """
## INTRO
Feixiao opens strong.
**ULTIMATE**
Starting with the ultimate.
""",
        encoding="utf-8",
    )

    tokens = [
        TranscriptWord("Feixiao", "feixiao", 0.0, 0.3),
        TranscriptWord("opens", "opens", 0.3, 0.6),
        TranscriptWord("strong", "strong", 0.6, 0.9),
        TranscriptWord("Starting", "starting", 1.2, 1.5),
        TranscriptWord("with", "with", 1.5, 1.7),
        TranscriptWord("the", "the", 1.7, 1.9),
        TranscriptWord("ultimate", "ultimate", 1.9, 2.2),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("sample.wav"),
        transcription=tokens,
        similarity_threshold=0.6,
    )

    assert [span.text for span in result.aligned] == [
        "Feixiao opens strong.",
        "Starting with the ultimate.",
    ]
    assert result.skipped_script_ids == []


def test_alignment_prunes_overlapping_tokens(tmp_path):
    script = tmp_path / "tight.md"
    script.write_text(
        "Line one ends here.\nLine two begins now.",
        encoding="utf-8",
    )

    tokens = [
        TranscriptWord("Line", "line", 0.0, 0.2),
        TranscriptWord("one", "one", 0.2, 0.4),
        TranscriptWord("ends", "ends", 0.4, 0.6),
        TranscriptWord("here", "here", 0.6, 0.8),
        TranscriptWord("Line", "line", 0.8, 1.0),
        TranscriptWord("two", "two", 1.0, 1.2),
        TranscriptWord("begins", "begins", 1.2, 1.4),
        TranscriptWord("now", "now", 1.4, 1.6),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("tight.wav"),
        transcription=tokens,
        similarity_threshold=0.7,
        coverage_threshold=0.7,
    )

    assert [
        " ".join(word["text"] for word in span.words)
        for span in result.aligned
    ] == [
        "Line one ends here",
        "Line two begins now",
    ]


def test_transcript_round_trip(tmp_path):
    tokens = [
        TranscriptWord("Hello", "hello", 0.0, 0.2),
        TranscriptWord("world", "world", 0.2, 0.5),
    ]
    transcript_path = tmp_path / "sample.transcript.json"
    write_transcript(tokens, transcript_path)

    loaded = load_transcript(transcript_path)
    assert [tok.text for tok in loaded] == ["Hello", "world"]
    assert [tok.norm for tok in loaded] == ["hello", "world"]


def test_unicode_punctuation_normalized_for_alignment(tmp_path):
    script = tmp_path / "script.md"
    script.write_text("My name is Neo Star Impact. Let’s begin!", encoding="utf-8")

    tokens = [
        TranscriptWord("My", "my", 0.0, 0.2),
        TranscriptWord("name", "name", 0.2, 0.4),
        TranscriptWord("is", "is", 0.4, 0.6),
        TranscriptWord("Neo", "neo", 0.6, 0.9),
        TranscriptWord("Star", "star", 0.9, 1.2),
        TranscriptWord("Impact", "impact", 1.2, 1.5),
        TranscriptWord("let's", "let's", 2.0, 2.3),
        TranscriptWord("begin", "begin", 2.3, 2.6),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("sample.wav"),
        transcription=tokens,
        similarity_threshold=0.6,
    )

    assert [span.id for span in result.aligned] == ["L001", "L002"]
    assert not result.skipped_script_ids



def test_percent_symbol_preserved_in_word_spans(tmp_path):
    script = tmp_path / "percent.md"
    script.write_text(
        "It contributes to 70% of her overall damage.",
        encoding="utf-8",
    )

    tokens = [
        TranscriptWord("It", "it", 0.0, 0.2),
        TranscriptWord("contributes", "contributes", 0.2, 0.6),
        TranscriptWord("to", "to", 0.6, 0.8),
        TranscriptWord("70", "70", 0.8, 1.0),
        TranscriptWord("percent", "percent", 1.0, 1.3),
        TranscriptWord("of", "of", 1.3, 1.5),
        TranscriptWord("her", "her", 1.5, 1.7),
        TranscriptWord("overall", "overall", 1.7, 2.0),
        TranscriptWord("damage", "damage", 2.0, 2.3),
    ]

    result = align_script_to_audio(
        script_path=script,
        audio_path=Path("percent.wav"),
        transcription=tokens,
        similarity_threshold=0.6,
    )

    span = result.aligned[0]
    assert any(word["text"] == "70%" for word in span.words)
