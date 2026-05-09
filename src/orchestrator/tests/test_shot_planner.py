from __future__ import annotations

import json

from src.orchestrator import models
from src.orchestrator.shot_planner import ShotPlanner


def test_shot_planner_reuses_cached_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("gemini_api_key", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    plans_dir = tmp_path / "shot_plans"
    plans_dir.mkdir()
    cached = models.ShotPlan(
        beat_id="B001",
        shot_instructions=[
            models.ShotInstruction(
                shot_id="B001-S01",
                narration_ids=["L001"],
                vo_excerpt="cached narration",
                visual_goal="cached visual goal",
                keywords=["cached"],
                required_events=[],
                preferred_assets=["asset-a"],
                min_clips=1,
                max_clips=1,
                coverage_note="cached coverage",
            )
        ],
    )
    try:
        payload = cached.model_dump()  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - pydantic v1 fallback
        payload = cached.dict()
    (plans_dir / "B001.json").write_text(json.dumps(payload), encoding="utf-8")

    planner = ShotPlanner(shot_plans_dir=plans_dir)
    plan = planner.plan_for_beat(
        beat=models.Beat(
            id="B001",
            segment_ids=["L001"],
            visual_intent="new visual intent",
            pacing="normal",
            search_queries=["new"],
        ),
        segment_context=[{"id": "L001", "text": "new narration"}],
    )

    assert plan.shot_instructions[0].visual_goal == "cached visual goal"
