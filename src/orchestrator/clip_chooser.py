"""LLM-based clip chooser that evaluates real video slices."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from src.orchestrator.clip_slices import ClipSlice
from src.services.encoding.ffmpeg_service import FfmpegEncodingService, EncodingServiceError

LOGGER = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")
_TIMESTAMP_EPSILON = 0.01
_COMBAT_TERMS = {
    "attack",
    "battle",
    "break",
    "combat",
    "damage",
    "enemy",
    "fight",
    "skill",
    "ultimate",
}
_UI_TERMS = {
    "build",
    "button",
    "gear",
    "menu",
    "relic",
    "screen",
    "stats",
    "trace",
    "traces",
    "ui",
}


class ClipChoiceResponse(BaseModel):
    """Schema returned by Gemini."""

    found_match: bool = Field(..., description="Set false when none of the candidates fit")
    selected_asset_id: Optional[str] = Field(None, description="Asset chosen by the model")
    start_time: Optional[float] = Field(None, description="Recommended in-point inside the asset")
    end_time: Optional[float] = Field(None, description="Recommended out-point inside the asset")
    reasoning: str = Field(..., description="Brief explanation for QA")


class ClipSliceDecision(BaseModel):
    asset_id: str
    start_time: float
    end_time: float
    reasoning: str


@dataclass
class _CandidatePayload:
    candidate: ClipSlice
    video_part: dict[str, Any]
    upload_name: Optional[str]
    temp_path: Path


class ClipChooser:
    """Delegates final slice selection to Gemini with deterministic safety gates."""

    def __init__(self, *, model_name: str | None = None) -> None:
        api_key = (
            os.environ.get("gemini_api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        self.client = genai.Client(api_key=api_key) if api_key else None
        self.model_name = model_name or os.environ.get("GEMINI_MODEL_EDITOR", "gemini-2.5-flash")
        self.encoder = FfmpegEncodingService()
        self.last_failure_reason: str | None = None

    def choose_clip(
        self,
        *,
        vo_excerpt: str,
        vo_duration: float,
        coverage_note: str,
        beat_intent: str,
        visual_goal: str = "",
        required_events: Sequence[str] = (),
        keywords: Sequence[str] = (),
        candidates: Sequence[ClipSlice],
        prefer_metadata: bool = False,
    ) -> ClipSliceDecision | None:
        self.last_failure_reason = None
        if not candidates:
            self.last_failure_reason = "No candidates available for clip selection"
            return None

        if prefer_metadata:
            return self._deterministic_decision(
                candidates,
                vo_excerpt=vo_excerpt,
                vo_duration=vo_duration,
                visual_goal=visual_goal,
                coverage_note=coverage_note,
                beat_intent=beat_intent,
                required_events=required_events,
                keywords=keywords,
                reason="validated metadata candidate",
            )

        if not self.client:
            return self._deterministic_decision(
                candidates,
                vo_excerpt=vo_excerpt,
                vo_duration=vo_duration,
                visual_goal=visual_goal,
                coverage_note=coverage_note,
                beat_intent=beat_intent,
                required_events=required_events,
                keywords=keywords,
                reason="Gemini client unavailable",
            )

        payloads = self._prepare_payloads(candidates)
        if not payloads:
            return self._deterministic_decision(
                candidates,
                vo_excerpt=vo_excerpt,
                vo_duration=vo_duration,
                visual_goal=visual_goal,
                coverage_note=coverage_note,
                beat_intent=beat_intent,
                required_events=required_events,
                keywords=keywords,
                reason="Unable to prepare video slices",
            )

        prompt_header = self._build_prompt_header(
            vo_excerpt=vo_excerpt,
            vo_duration=vo_duration,
            visual_goal=visual_goal,
            coverage_note=coverage_note,
            beat_intent=beat_intent,
            required_events=required_events,
            keywords=keywords,
        )
        parts: list[Any] = [types.Part(text=prompt_header)]
        for idx, payload in enumerate(payloads, start=1):
            candidate = payload.candidate
            description_lines = [
                f"Candidate {idx}: Asset {candidate.asset_id}",
                f"Window {candidate.window_start:.2f}-{candidate.window_end:.2f}s | Slice {candidate.slice_start:.2f}-{candidate.slice_end:.2f}s",
                f"Event summary: {candidate.event_summary}",
            ]
            if candidate.event_tags:
                description_lines.append(f"Tags: {', '.join(candidate.event_tags)}")
            if candidate.description:
                description_lines.append(f"Window context: {candidate.description}")
            parts.append(types.Part(text="\n".join(description_lines)))
            parts.append(payload.video_part)
        parts.append(
            types.Part(
                text=(
                    "Instructions: Review every clip, pick the single best candidate, and return the asset ID plus the "
                    "absolute start/end timestamps (seconds within the source asset). Your in/out choices must stay "
                    "inside the provided slice range, but you may tighten it to match the narration pacing."
                )
            )
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[{"role": "user", "parts": parts}],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ClipChoiceResponse,
                    temperature=0.15,
                ),
            )
        except Exception as exc:  # pragma: no cover - external call
            self._cleanup_payloads(payloads)
            return self._deterministic_decision(
                candidates,
                vo_excerpt=vo_excerpt,
                vo_duration=vo_duration,
                visual_goal=visual_goal,
                coverage_note=coverage_note,
                beat_intent=beat_intent,
                required_events=required_events,
                keywords=keywords,
                reason=f"ClipChooser error: {exc}",
            )

        self._cleanup_payloads(payloads)

        parsed: ClipChoiceResponse = response.parsed
        if not parsed.found_match or not parsed.selected_asset_id:
            return self._deterministic_decision(
                candidates,
                vo_excerpt=vo_excerpt,
                vo_duration=vo_duration,
                visual_goal=visual_goal,
                coverage_note=coverage_note,
                beat_intent=beat_intent,
                required_events=required_events,
                keywords=keywords,
                reason=parsed.reasoning,
            )

        for candidate in candidates:
            if candidate.asset_id != parsed.selected_asset_id:
                continue
            start = parsed.start_time if parsed.start_time is not None else candidate.slice_start
            end = parsed.end_time if parsed.end_time is not None else candidate.slice_end
            if parsed.start_time is not None and _is_outside_range(
                parsed.start_time,
                candidate.slice_start,
                candidate.slice_end,
            ):
                LOGGER.warning(
                    "ClipChooser: requested in-point %.2fs is outside slice %.2f-%.2fs for asset %s",
                    parsed.start_time,
                    candidate.slice_start,
                    candidate.slice_end,
                    candidate.asset_id,
                )
            if parsed.end_time is not None and _is_outside_range(
                parsed.end_time,
                candidate.slice_start,
                candidate.slice_end,
            ):
                LOGGER.warning(
                    "ClipChooser: requested out-point %.2fs is outside slice %.2f-%.2fs for asset %s",
                    parsed.end_time,
                    candidate.slice_start,
                    candidate.slice_end,
                    candidate.asset_id,
                )

            clamped_start = _clamp_timestamp(start, candidate.slice_start, candidate.slice_end)
            clamped_end = _clamp_timestamp(end, candidate.slice_start, candidate.slice_end)
            if clamped_end <= clamped_start:
                clamped_end = min(candidate.slice_end, clamped_start + max(candidate.duration, 0.1))

            failures = self._quality_failures(
                candidate,
                start_time=clamped_start,
                end_time=clamped_end,
                vo_excerpt=vo_excerpt,
                vo_duration=vo_duration,
                visual_goal=visual_goal,
                coverage_note=coverage_note,
                beat_intent=beat_intent,
                required_events=required_events,
            )
            if failures:
                LOGGER.warning(
                    "ClipChooser: rejecting Gemini selection for asset %s: %s",
                    candidate.asset_id,
                    "; ".join(failures),
                )
                return self._deterministic_decision(
                    candidates,
                    vo_excerpt=vo_excerpt,
                    vo_duration=vo_duration,
                    visual_goal=visual_goal,
                    coverage_note=coverage_note,
                    beat_intent=beat_intent,
                    required_events=required_events,
                    keywords=keywords,
                    reason="Gemini selection rejected by safety gates: " + "; ".join(failures),
                    skipped_asset_ids={candidate.asset_id},
                )

            return ClipSliceDecision(
                asset_id=candidate.asset_id,
                start_time=clamped_start,
                end_time=clamped_end,
                reasoning=parsed.reasoning,
            )

        return self._deterministic_decision(
            candidates,
            vo_excerpt=vo_excerpt,
            vo_duration=vo_duration,
            visual_goal=visual_goal,
            coverage_note=coverage_note,
            beat_intent=beat_intent,
            required_events=required_events,
            keywords=keywords,
            reason="LLM referenced unknown asset",
        )

    def _deterministic_decision(
        self,
        candidates: Sequence[ClipSlice],
        *,
        vo_excerpt: str,
        vo_duration: float,
        visual_goal: str,
        coverage_note: str,
        beat_intent: str,
        required_events: Sequence[str],
        keywords: Sequence[str],
        reason: str,
        skipped_asset_ids: set[str] | None = None,
    ) -> ClipSliceDecision | None:
        del keywords
        skipped = skipped_asset_ids or set()
        rejection_reasons: list[str] = []

        for candidate in candidates:
            if candidate.asset_id in skipped:
                continue
            failures = self._quality_failures(
                candidate,
                start_time=candidate.slice_start,
                end_time=candidate.slice_end,
                vo_excerpt=vo_excerpt,
                vo_duration=vo_duration,
                visual_goal=visual_goal,
                coverage_note=coverage_note,
                beat_intent=beat_intent,
                required_events=required_events,
            )
            if failures:
                rejection_reasons.append(f"{candidate.asset_id}: {', '.join(failures)}")
                continue
            return ClipSliceDecision(
                asset_id=candidate.asset_id,
                start_time=candidate.slice_start,
                end_time=candidate.slice_end,
                reasoning=f"Deterministic safety-pass selection: {reason}",
            )

        details = "; ".join(rejection_reasons[:3])
        self.last_failure_reason = reason if not details else f"{reason}; {details}"
        LOGGER.warning("ClipChooser: no acceptable candidate remained: %s", self.last_failure_reason)
        return None

    def _quality_failures(
        self,
        candidate: ClipSlice,
        *,
        start_time: float,
        end_time: float,
        vo_excerpt: str,
        vo_duration: float,
        visual_goal: str,
        coverage_note: str,
        beat_intent: str,
        required_events: Sequence[str],
    ) -> list[str]:
        failures: list[str] = []
        clip_duration = max(end_time - start_time, 0.0)
        min_duration = self._minimum_duration(vo_duration)
        if clip_duration < min_duration:
            failures.append(
                f"duration {clip_duration:.2f}s is below minimum {min_duration:.2f}s for VO {vo_duration:.2f}s"
            )

        candidate_text = " ".join(
            filter(None, [candidate.event_summary, candidate.description, " ".join(candidate.event_tags)])
        )
        candidate_mode = _classify_visual_mode(candidate_text)
        desired_mode = _desired_visual_mode(
            visual_goal=visual_goal,
            fallback_text=" ".join(filter(None, [vo_excerpt, coverage_note])),
        )
        if desired_mode != "neutral" and candidate_mode != "neutral" and desired_mode != candidate_mode:
            failures.append(f"visual mode mismatch ({candidate_mode} vs expected {desired_mode})")

        if required_events and not _matches_any_constraint(required_events, candidate_text.lower()):
            failures.append("missing required event or entity cues")

        return failures

    def _minimum_duration(self, vo_duration: float) -> float:
        if vo_duration <= 0.0:
            return 0.1
        if vo_duration >= 5.0:
            return max(1.5, vo_duration * 0.4)
        if vo_duration >= 3.0:
            return max(1.0, vo_duration * 0.35)
        return max(0.5, vo_duration * 0.25)

    def _build_prompt_header(
        self,
        *,
        vo_excerpt: str,
        vo_duration: float,
        visual_goal: str,
        coverage_note: str,
        beat_intent: str,
        required_events: Sequence[str],
        keywords: Sequence[str],
    ) -> str:
        lines: list[str] = []
        lines.append("Match the narration with the footage and stay within the allowed windows.")
        lines.append("")
        lines.append(f"Narration: {vo_excerpt.strip()}")
        lines.append(f"Target duration: ~{vo_duration:.2f}s (keep within +/-0.30s if you need breathing room)")
        if visual_goal:
            lines.append(f"Visual goal: {visual_goal}")
        if coverage_note:
            lines.append(f"Coverage intent: {coverage_note}")
        if beat_intent:
            lines.append(f"Beat visual intent: {beat_intent}")
        if required_events:
            lines.append(f"Required events/entities: {', '.join(required_events)}")
        if keywords:
            lines.append(f"Important keywords: {', '.join(keywords)}")
        lines.append("")
        return "\n".join(lines)

    def _prepare_payloads(self, candidates: Sequence[ClipSlice]) -> list[_CandidatePayload]:
        payloads: list[_CandidatePayload] = []
        if not self.client:
            return payloads
        for candidate in candidates:
            payload = self._prepare_single_payload(candidate)
            if payload:
                payloads.append(payload)
        return payloads

    def _prepare_single_payload(self, candidate: ClipSlice) -> Optional[_CandidatePayload]:
        if not candidate.source_path:
            print(f"[ClipChooser] Missing source path for asset {candidate.asset_id}")
            return None
        duration = max(candidate.duration, 0.1)
        try:
            temp_target = Path(tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name)
            clip_path = self.encoder.cut_segment(
                Path(candidate.source_path),
                start=candidate.slice_start,
                duration=duration,
                output_path=temp_target,
            )
        except (EncodingServiceError, FileNotFoundError) as exc:
            print(f"[ClipChooser] Failed to cut slice for {candidate.asset_id}: {exc}")
            return None

        try:
            upload = self.client.files.upload(file=str(clip_path))
        except Exception as exc:  # pragma: no cover - network failure
            print(f"[ClipChooser] Upload failed for {candidate.asset_id}: {exc}")
            clip_path.unlink(missing_ok=True)
            return None

        upload_name = getattr(upload, "name", None)
        if upload_name:
            try:
                self._wait_for_file_active(upload_name)
            except Exception as exc:  # pragma: no cover - network failure
                print(f"[ClipChooser] Upload activation failed for {candidate.asset_id}: {exc}")
                clip_path.unlink(missing_ok=True)
                try:
                    self.client.files.delete(name=upload_name)
                except Exception:
                    pass
                return None

        video_part = {
            "file_data": {
                "file_uri": upload.uri,
                "mime_type": upload.mime_type,
            }
        }
        return _CandidatePayload(candidate=candidate, video_part=video_part, upload_name=upload_name, temp_path=clip_path)

    def _wait_for_file_active(self, file_name: str, timeout: float = 180.0) -> None:
        if not self.client:
            return
        start = time.time()
        while True:
            file_state = self.client.files.get(name=file_name)
            state_name = getattr(getattr(file_state, "state", None), "name", None)
            if state_name in {None, "ACTIVE"}:
                return
            if state_name == "FAILED":
                raise RuntimeError(f"Gemini file {file_name} failed to process")
            if time.time() - start > timeout:
                raise TimeoutError(f"Gemini file {file_name} did not become active within {timeout} seconds")
            time.sleep(1.0)

    def _cleanup_payloads(self, payloads: Sequence[_CandidatePayload]) -> None:
        for payload in payloads:
            payload.temp_path.unlink(missing_ok=True)
            if self.client and payload.upload_name:
                try:
                    self.client.files.delete(name=payload.upload_name)
                except Exception:
                    pass


def _matches_any_constraint(phrases: Sequence[str], haystack: str) -> bool:
    normalized_haystack = haystack.lower()
    for phrase in phrases:
        if not phrase:
            continue
        normalized_phrase = phrase.lower()
        if normalized_phrase in normalized_haystack:
            return True
        phrase_tokens = [token for token in _TOKEN_PATTERN.findall(normalized_phrase) if len(token) >= 3]
        if phrase_tokens and all(token in normalized_haystack for token in phrase_tokens):
            return True
    return False


def _classify_visual_mode(text: str) -> str:
    tokens = {token.lower() for token in _TOKEN_PATTERN.findall(text)}
    if not tokens:
        return "neutral"
    combat_hits = len(tokens & _COMBAT_TERMS)
    ui_hits = len(tokens & _UI_TERMS)
    if combat_hits == ui_hits:
        return "neutral"
    return "combat" if combat_hits > ui_hits else "ui"


def _desired_visual_mode(*, visual_goal: str, fallback_text: str) -> str:
    goal_mode = _classify_visual_mode(visual_goal)
    if goal_mode != "neutral":
        return goal_mode
    return _classify_visual_mode(fallback_text)


def _is_outside_range(value: float, start: float, end: float) -> bool:
    return value < start - _TIMESTAMP_EPSILON or value > end + _TIMESTAMP_EPSILON


def _clamp_timestamp(value: float, start: float, end: float) -> float:
    if abs(value - start) <= _TIMESTAMP_EPSILON:
        return start
    if abs(value - end) <= _TIMESTAMP_EPSILON:
        return end
    return max(start, min(value, end))
