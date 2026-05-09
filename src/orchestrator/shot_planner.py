"""LLM-powered shot planning utilities."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from google import genai
from google.genai import types

from src.orchestrator import models
from src.orchestrator.clients import gemini_files
from src.orchestrator.retrieval import footage_manifest

LOGGER = logging.getLogger(__name__)


class ShotPlanner:
    """Generates shot directives for a beat using Gemini (with fallback)."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        manifest_path: Optional[Path] = None,
        manifest_handle_path: Optional[Path] = None,
        shot_plans_dir: Optional[Path] = None,
    ) -> None:
        api_key = (
            os.environ.get("gemini_api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        self.client = None
        self.model_name = model_name or os.environ.get(
            "GEMINI_MODEL_EDITOR", "gemini-2.5-flash"
        )
        self.profile_name = os.environ.get("EVENT_METADATA_PROFILE") or os.environ.get("VIDEO_EMBED_PROFILE")
        if self.profile_name is None:
            try:  # pragma: no cover - optional dependency
                from gateway.vision.event_metadata.config import get_settings

                self.profile_name = get_settings().profile
            except Exception as exc:
                LOGGER.debug("ShotPlanner could not resolve profile: %s", exc)

        if api_key:
            self.client = genai.Client(api_key=api_key)
        else:
            print("[ShotPlanner] No Gemini API key detected; using fallback planner.")

        self.manifest_path = manifest_path
        self.manifest_handle_path = manifest_handle_path
        self.manifest_entries: List[Dict[str, Any]] = []
        self._load_manifest_entries()
        self._recent_asset_suggestions: Dict[str, List[str]] = {}
        self.file_attachments: List[Any] = []
        self.manifest_hint = ""
        self.shot_plans_dir = shot_plans_dir

    def plan_for_beat(
        self,
        *,
        beat: models.Beat,
        segment_context: Sequence[Dict[str, Any]],
        file_attachments: Optional[Sequence[Any]] = None,
        manifest_hint: Optional[str] = None,
    ) -> models.ShotPlan:
        """Ask Gemini to design a visual narration plan for the supplied beat."""

        cached_plan = self._load_persisted_shot_plan(beat.id)
        if cached_plan is not None:
            print(f"[ShotPlanner] Reusing cached shot plan for {beat.id}")
            return cached_plan

        relevant_entries = self._select_relevant_footage_entries(beat, segment_context)
        self._log_manifest_usage(beat, relevant_entries)

        if self.client is not None:
            prompt = self._build_prompt(
                beat=beat,
                segment_context=segment_context,
                manifest_hint=manifest_hint or self.manifest_hint,
            )
            try:
                manifest_attachments = self._ensure_manifest_attachment()
            except Exception as exc:  # pragma: no cover - configuration issues
                raise RuntimeError(f"ShotPlanner cannot attach manifest: {exc}") from exc

            attachments = list(manifest_attachments)
            if file_attachments:
                attachments.extend(file_attachments)
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=[*attachments, prompt] if attachments else [prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=models.ShotPlan,
                        temperature=0.2,
                    ),
                )
                plan = response.parsed
                if plan and plan.shot_instructions:
                    plan = self._apply_asset_suggestions(plan)
                    self._persist_shot_plan(beat.id, plan)
                    return plan
            except Exception as exc:  # pragma: no cover - network/LLM failures
                print(f"[ShotPlanner] Gemini plan failed: {exc}")

        # Fallback path: create literal shot-per-line plan
        plan = self._fallback_plan(beat=beat, segment_context=segment_context)
        plan = self._apply_asset_suggestions(plan)
        self._persist_shot_plan(beat.id, plan)
        return plan

    def _build_prompt(
        self,
        *,
        beat: models.Beat,
        segment_context: Sequence[Dict[str, Any]],
        manifest_hint: str = "",
    ) -> str:
        lines: List[str] = []
        lines.append(
            "You are a senior video director building a shot list for an educational gameplay guide."
        )
        lines.append(
            "Each shot must reference concrete narration segments and explain what the viewer sees."
        )
        lines.append(
            "CRITICAL: You MUST create enough shots to cover EVERY narration segment listed below. Do not skip any segment."
        )
        lines.append("The narration beat we are covering:")
        lines.append(f"Beat ID: {beat.id}")
        lines.append(f"Visual Intent: {beat.visual_intent}")
        lines.append(f"Suggested Search Queries: {', '.join(beat.search_queries)}")
        lines.append("\nNarration Segments (chronological):")
        for seg in segment_context:
            seg_id = seg.get("id")
            text = seg.get("text", "")
            lines.append(f"- {seg_id}: {text}")
        vector_lines = self._vector_window_samples(beat)
        if vector_lines:
            lines.append("\nEvent metadata windows already indexed (useful vocabulary):")
            for summary in vector_lines:
                lines.append(f"- {summary}")

        if manifest_hint:
            lines.append("\nAttached manifest (Gemini Files):")
            lines.append(f"- {manifest_hint}")
        else:
            lines.append("\nAttached manifest: (none)")

        lines.append(
            "\nOutput requirements:\n"
            "1. Merge sentences only when they describe the same exact visual idea."
            "2. Each shot must state what gameplay moment to show (character, ability, UI, etc.)."
            "3. Provide keywords or events to help retrieval find the footage."
            "4. Set min/max clips to 1-3 depending on how much coverage the narration needs."
            "5. coverage_note explains why this shot supports the narration."
            "6. If certain footage summaries clearly match, call them out explicitly and include their asset_id inside preferred_assets."
            "Return JSON following the ShotPlan schema."
        )
        return "\n".join(lines)

    def _fallback_plan(
        self,
        *,
        beat: models.Beat,
        segment_context: Sequence[Dict[str, Any]],
    ) -> models.ShotPlan:
        instructions: List[models.ShotInstruction] = []
        for idx, seg in enumerate(segment_context, start=1):
            seg_id = seg.get("id", f"SEG{idx:02d}")
            text = seg.get("text", "")
            instructions.append(
                models.ShotInstruction(
                    shot_id=seg_id,
                    narration_ids=[seg_id],
                    vo_excerpt=text,
                    visual_goal=f"Literal visualization of: {text}",
                    keywords=list(beat.search_queries),
                    required_events=[],
                    preferred_assets=[],
                    min_clips=1,
                    max_clips=1,
                    coverage_note=f"Direct coverage for segment {seg_id}",
                )
            )
        return models.ShotPlan(beat_id=beat.id, shot_instructions=instructions)

    def _vector_window_samples(self, beat: models.Beat, limit: int = 3) -> List[str]:
        if not self.profile_name:
            return []
        query = " ".join(beat.search_queries).strip() or beat.visual_intent
        if not query:
            return []
        try:  # pragma: no cover - relies on local metadata DB
            from gateway.vision.event_metadata.search import search_events

            results = search_events(
                query,
                profile_name=self.profile_name,
                limit=limit,
            )
        except Exception as exc:
            LOGGER.debug("ShotPlanner could not sample event metadata windows: %s", exc)
            return []

        summaries: List[str] = []
        for res in results:
            tags = ", ".join(res.tags or []) or "(no tags)"
            caption = (res.summary or "").strip()
            preview = caption[:120] + ("..." if len(caption) > 120 else "")
            summaries.append(
                f"{res.video_id} [{res.start_s:.1f}-{res.end_s:.1f}s]: {tags} -> {preview}"
            )
        return summaries

    def _select_relevant_footage_entries(
        self,
        beat: models.Beat,
        segment_context: Sequence[Dict[str, Any]],
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        if not self.manifest_entries:
            self._recent_asset_suggestions.pop(beat.id, None)
            return []

        text_blobs: List[str] = []
        if beat.visual_intent:
            text_blobs.append(beat.visual_intent)
        text_blobs.extend(beat.search_queries)
        for seg in segment_context:
            text_blobs.append(seg.get("text", ""))
        corpus = " ".join(text_blobs).lower()
        tokens = {tok for tok in re.split(r"\W+", corpus) if len(tok) >= 4}
        if not tokens:
            return []

        ranked: List[tuple[int, Dict[str, Any]]] = []
        for entry in self.manifest_entries:
            haystack_parts = [entry.get("summary", ""), " ".join(entry.get("tags", []))]
            haystack = " ".join(haystack_parts).lower()
            score = sum(1 for token in tokens if token in haystack)
            if score > 0:
                ranked.append((score, entry))

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = [entry for _, entry in ranked[:limit]]
        self._recent_asset_suggestions[beat.id] = [e.get("asset_id") for e in selected if e.get("asset_id")]
        return selected

    def _apply_asset_suggestions(self, plan: models.ShotPlan) -> models.ShotPlan:
        suggestions = self._recent_asset_suggestions.get(plan.beat_id)
        if not suggestions:
            return plan

        updated_instructions: List[models.ShotInstruction] = []
        for instruction in plan.shot_instructions:
            existing = list(instruction.preferred_assets or [])
            combined = []
            for asset in existing + suggestions:
                if asset and asset not in combined:
                    combined.append(asset)
            if combined == existing or not combined:
                updated_instructions.append(instruction)
                continue
            try:
                updated = instruction.model_copy(update={"preferred_assets": combined})  # type: ignore[attr-defined]
            except AttributeError:  # pragma: no cover - pydantic v1 fallback
                updated = instruction.copy(update={"preferred_assets": combined})
            updated_instructions.append(updated)

        try:
            return plan.model_copy(update={"shot_instructions": updated_instructions})  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover
            return plan.copy(update={"shot_instructions": updated_instructions})

    def _persist_shot_plan(self, beat_id: str, plan: models.ShotPlan) -> None:
        if not self.shot_plans_dir:
            return
        try:
            self.shot_plans_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOGGER.warning("ShotPlanner could not create shot plan directory %s: %s", self.shot_plans_dir, exc)
            return
        path = self.shot_plans_dir / f"{beat_id}.json"
        try:
            data = plan.model_dump()  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - pydantic v1 fallback
            data = plan.dict()
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        except OSError as exc:
            LOGGER.warning("ShotPlanner failed to write shot plan %s: %s", path, exc)

    def _load_persisted_shot_plan(self, beat_id: str) -> Optional[models.ShotPlan]:
        if not self.shot_plans_dir:
            return None
        path = self.shot_plans_dir / f"{beat_id}.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            try:
                plan = models.ShotPlan.model_validate(data)  # type: ignore[attr-defined]
            except AttributeError:  # pragma: no cover - pydantic v1 fallback
                plan = models.ShotPlan.parse_obj(data)
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.warning("ShotPlanner failed to read cached shot plan %s: %s", path, exc)
            return None
        if plan.beat_id != beat_id or not plan.shot_instructions:
            LOGGER.warning(
                "ShotPlanner ignored cached shot plan %s: expected beat %s, got %s with %d shots",
                path,
                beat_id,
                plan.beat_id,
                len(plan.shot_instructions),
            )
            return None
        return plan

    def _load_manifest_entries(self) -> None:
        if not self.manifest_path:
            LOGGER.warning("ShotPlanner created without manifest_path; manifest usage is required.")
            return

        try:
            self.manifest_entries = footage_manifest.read_manifest(self.manifest_path)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"ShotPlanner manifest missing: {self.manifest_path}"
            )
        except ValueError as exc:
            raise ValueError(f"ShotPlanner manifest invalid: {exc}") from exc

        preview = footage_manifest.format_manifest_preview(self.manifest_entries)
        print(
            f"[ShotPlanner] Loaded {len(self.manifest_entries)} manifest entries from {self.manifest_path}"
        )
        print(preview)

    def _ensure_manifest_attachment(self) -> List[Any]:
        if self.file_attachments:
            return self.file_attachments

        if not self.manifest_path:
            raise RuntimeError("manifest_path not configured")
        if self.client is None:
            raise RuntimeError("Gemini client unavailable for manifest upload")

        files_client = gemini_files.GeminiFilesClient()
        handle = None
        if self.manifest_handle_path:
            cached = gemini_files.load_handle(self.manifest_handle_path)
            if cached and gemini_files.handle_matches_file(cached, self.manifest_path):
                handle = cached
        if handle is None:
            handle = files_client.upload(self.manifest_path)
            if self.manifest_handle_path:
                gemini_files.save_handle(handle, self.manifest_handle_path)
            print(
                f"[ShotPlanner] Uploaded manifest ({handle.size_bytes} bytes) -> {handle.gemini_name}"
            )
        else:
            print(
                f"[ShotPlanner] Reusing manifest handle {handle.gemini_name} (expires {handle.expire_time or '≈48h'})"
            )

        file_obj = files_client.get(handle.gemini_name)
        self.manifest_hint = (
            f"Handle {handle.gemini_name} | size={handle.size_bytes} bytes | "
            f"expires {handle.expire_time or '≈48h from upload'}"
        )
        self.file_attachments = [file_obj]
        return self.file_attachments

    def _log_manifest_usage(self, beat: models.Beat, entries: Sequence[Dict[str, Any]]) -> None:
        if not entries:
            print(f"[ShotPlanner] Beat {beat.id}: no manifest entries matched keywords.")
            return

        print(f"[ShotPlanner] Beat {beat.id}: referencing manifest entries:")
        for entry in entries[:5]:
            asset = entry.get("asset_id") or entry.get("source_path") or "(unknown asset)"
            summary = (entry.get("summary") or "").strip()
            if len(summary) > 160:
                summary = summary[:160].rstrip() + "…"
            print(f"  - {asset}: {summary}")
        remainder = len(entries) - min(len(entries), 5)
        if remainder > 0:
            print(f"  - …and {remainder} more candidate entries")
