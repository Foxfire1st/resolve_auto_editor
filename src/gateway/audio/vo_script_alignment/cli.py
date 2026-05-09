"""Typer CLI entry point for script↔VO alignment.

USAGE:
    python -m gateway.audio.vo_script_alignment.cli \\
        --script ./script.md \\
        --audio ./voice_over.wav \\
        --out ./alignment.json \\
        --report ./alignment_report.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .aligner import (
    DEFAULT_MODEL_SIZE,
    align_script_to_audio,
    align_script_to_audio_chunked,
    extract_terms_from_script,
    load_transcript,
    transcribe_audio,
    write_transcript,
)

app = typer.Typer(add_completion=False, help="Align Markdown scripts to VO audio.")


@app.command()
def align(
    script: Path = typer.Option(..., exists=True, readable=True, help="Script file (.md/.txt)."),
    audio: Path = typer.Option(..., exists=True, readable=True, help="Voice-over audio file."),
    out: Path = typer.Option(..., help="Path to alignment JSON output."),
    report: Optional[Path] = typer.Option(None, help="Optional path for detailed report JSON."),
    model: str = typer.Option(
        DEFAULT_MODEL_SIZE,
        help="stable-ts/Whisper model size (e.g. small, medium, large-v3).",
    ),
    device: Optional[str] = typer.Option(
        None, help="Device override for stable-ts (cpu, cuda, dml, auto)."
    ),
    terms: Optional[Path] = typer.Option(None, help="Optional domain terms file to boost pronunciation."),
    auto_terms: bool = typer.Option(False, help="Auto-derive terms from the script to bias transcription."),
    terms_out: Optional[Path] = typer.Option(
        None, help="Optional path to write the derived terms when using --auto-terms."
    ),
    script_prompt: bool = typer.Option(
        False, help="Use the full script text as the Whisper initial prompt (overrides terms/auto-terms)."
    ),
    similarity: float = typer.Option(0.72, min=0.0, max=1.0, help="SequenceMatcher similarity threshold."),
    coverage: float = typer.Option(0.6, min=0.0, max=1.0, help="Token coverage threshold."),
    chunk_seconds: Optional[float] = typer.Option(
        None, help="If provided, process the audio in overlapping chunks of this length (seconds)."
    ),
    chunk_overlap: float = typer.Option(
        1.0, min=0.0, help="Overlap (seconds) between chunks when chunked processing is enabled."
    ),
    transcript_out: Optional[Path] = typer.Option(
        None, help="Optional path to write the raw transcript tokens (JSON)."
    ),
    transcript: Optional[Path] = typer.Option(
        None,
        exists=True,
        readable=True,
        help="Optional precomputed transcript JSON to align from (skips transcription).",
    ),
) -> None:
    """Run the alignment pipeline and write JSON artifacts."""

    if transcript and chunk_seconds:
        raise typer.BadParameter("--transcript cannot be combined with chunked alignment.")
    if transcript_out and chunk_seconds:
        raise typer.BadParameter("--transcript-out is only supported without --chunk-seconds.")

    terms_text: str | None = None
    if script_prompt:
        terms_text = script.read_text(encoding="utf-8")
    if auto_terms:
        terms_text, term_list = extract_terms_from_script(script)
        if terms_out:
            terms_out.write_text("\n".join(term_list), encoding="utf-8")
            typer.echo(f"Wrote {len(term_list)} derived terms to {terms_out}")
    precomputed_tokens = None
    if transcript:
        precomputed_tokens = load_transcript(transcript)
    elif not chunk_seconds:
        precomputed_tokens = transcribe_audio(
            audio_path=audio,
            model_size=model,
            device=device,
            terms_path=terms,
            terms_text=terms_text,
        )
        transcript_target = transcript_out or (out.parent / f"{out.stem}.transcript.json")
        write_transcript(precomputed_tokens, transcript_target)
        typer.echo(f"Wrote transcript tokens to {transcript_target}")

    if chunk_seconds:
        result = align_script_to_audio_chunked(
            script_path=script,
            audio_path=audio,
            chunk_seconds=chunk_seconds,
            overlap_seconds=chunk_overlap,
            model_size=model,
            device=device,
            terms_path=terms,
            terms_text=terms_text,
            similarity_threshold=similarity,
            coverage_threshold=coverage,
        )
    else:
        result = align_script_to_audio(
            script_path=script,
            audio_path=audio,
            transcription=precomputed_tokens,
            model_size=model,
            device=device,
            terms_path=terms,
            terms_text=terms_text,
            similarity_threshold=similarity,
            coverage_threshold=coverage,
        )
    out.write_text(result.to_json(indent=2), encoding="utf-8")
    typer.echo(f"Wrote {len(result.aligned)} aligned spans to {out}")

    if report:
        report_payload = {
            "aligned_count": len(result.aligned),
            "audio_only": [span.to_dict() for span in result.audio_only],
            "skipped_script_ids": result.skipped_script_ids,
        }
        report.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    app()
