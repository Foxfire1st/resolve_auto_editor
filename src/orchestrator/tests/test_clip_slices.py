from __future__ import annotations

from src.orchestrator import models
from src.orchestrator.clip_slices import generate_candidate_slices
from src.orchestrator.retrieval.event_parser import ClipEvent
from src.orchestrator.tool_bridge.retrieval_tools import SearchResult


def test_generate_candidate_slices_expands_beyond_window_when_asset_bounds_known(monkeypatch) -> None:
    beat = models.Beat(
        id="B010",
        segment_ids=["seg-10"],
        visual_intent="Show combat footage",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B010-S01",
        narration_ids=["n10"],
        vo_excerpt="Feixiao shows the full combat sequence.",
        visual_goal="Show a longer combat example",
        coverage_note="Use a longer slice that carries the whole explanation.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    result = SearchResult(
        asset_id="combat_clip",
        start=10.0,
        end=12.0,
        description="Feixiao uses her ultimate in battle.",
        score=0.9,
        source_path="combat_clip.mp4",
        tags=["combat", "ultimate"],
        events=[
            ClipEvent(
                offset=1.0,
                summary="Feixiao uses her ultimate in battle.",
                tags=["combat", "ultimate"],
                raw_text="synthetic",
            )
        ],
    )

    monkeypatch.setattr("src.orchestrator.clip_slices._get_source_asset_duration", lambda source_path: 20.0)

    slices = generate_candidate_slices(
        beat=beat,
        shot=shot,
        vo_range=vo_range,
        search_results=[result],
        max_candidates=3,
    )

    assert len(slices) == 2
    assert any(slice_.slice_start < 10.0 or slice_.slice_end > 12.0 for slice_ in slices)
    assert slices[0].variant == "medium"


def test_generate_candidate_slices_respects_asset_bounds(monkeypatch) -> None:
    beat = models.Beat(
        id="B011",
        segment_ids=["seg-11"],
        visual_intent="Show combat footage",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B011-S01",
        narration_ids=["n11"],
        vo_excerpt="Feixiao shows a short combat example.",
        visual_goal="Stay near the end of the clip",
        coverage_note="Do not exceed source bounds.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=5.0)
    result = SearchResult(
        asset_id="combat_clip",
        start=17.0,
        end=19.0,
        description="Feixiao uses her ultimate near the end of the clip.",
        score=0.9,
        source_path="combat_clip.mp4",
        tags=["combat", "ultimate"],
        events=[
            ClipEvent(
                offset=1.5,
                summary="Feixiao uses her ultimate near the end of the clip.",
                tags=["combat", "ultimate"],
                raw_text="synthetic",
            )
        ],
    )

    monkeypatch.setattr("src.orchestrator.clip_slices._get_source_asset_duration", lambda source_path: 20.0)

    slices = generate_candidate_slices(
        beat=beat,
        shot=shot,
        vo_range=vo_range,
        search_results=[result],
        max_candidates=3,
    )

    assert all(slice_.slice_start >= 0.0 for slice_ in slices)
    assert all(slice_.slice_end <= 20.0 for slice_ in slices)


def test_generate_candidate_slices_scales_stale_metadata_to_asset_duration(monkeypatch) -> None:
    beat = models.Beat(
        id="B033",
        segment_ids=["seg-33"],
        visual_intent="Show Topaz and Robin combat footage",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B033-S07",
        narration_ids=["n33"],
        vo_excerpt="Topaz is a great stacker for Feixiao.",
        visual_goal="Show Topaz and Robin combat synergy",
        coverage_note="Use the relevant late-fight Robin ultimate moment.",
        required_events=["Topaz", "Robin"],
        keywords=["Topaz", "Robin", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=8.92)
    result = SearchResult(
        asset_id="topazrobinfullE0S0",
        start=264.6,
        end=269.0,
        description="Robin uses her Ultimate while Topaz is in the team.",
        score=1.0,
        source_path="topazrobinfullE0S0.mp4",
        tags=["Topaz", "Robin", "ultimate"],
        events=[
            ClipEvent(
                offset=2.2,
                summary="Robin uses her Ultimate while Topaz is in the team.",
                tags=["Topaz", "Robin", "ultimate"],
                raw_text="stale-five-x-event",
            )
        ],
    )

    monkeypatch.setattr("src.orchestrator.clip_slices._get_source_asset_duration", lambda source_path: 177.916667)
    monkeypatch.setattr("src.orchestrator.clip_slices._get_metadata_time_extent", lambda source_path: 289.6)

    slices = generate_candidate_slices(
        beat=beat,
        shot=shot,
        vo_range=vo_range,
        search_results=[result],
        max_candidates=3,
    )

    assert slices
    assert all(slice_.slice_end <= 177.916667 for slice_ in slices)
    assert any("metadata time scaled" in slice_.reasoning for slice_ in slices)
    assert any(150.0 <= slice_.slice_start <= 170.0 for slice_ in slices)


def test_generate_candidate_slices_clamps_bad_event_offset_to_window(monkeypatch) -> None:
    beat = models.Beat(
        id="B012",
        segment_ids=["seg-12"],
        visual_intent="Show combat footage",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B012-S01",
        narration_ids=["n12"],
        vo_excerpt="Feixiao uses her ultimate.",
        visual_goal="Stay near the retrieved event window",
        coverage_note="Do not jump to an unrelated timestamp.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    result = SearchResult(
        asset_id="combat_clip",
        start=10.0,
        end=12.0,
        description="Feixiao uses her ultimate in battle.",
        score=0.9,
        source_path="combat_clip.mp4",
        tags=["combat", "ultimate"],
        events=[
            ClipEvent(
                offset=40.0,
                summary="Feixiao uses her ultimate in battle.",
                tags=["combat", "ultimate"],
                raw_text="bad-anchor",
            )
        ],
    )

    monkeypatch.setattr("src.orchestrator.clip_slices._get_source_asset_duration", lambda source_path: 100.0)

    slices = generate_candidate_slices(
        beat=beat,
        shot=shot,
        vo_range=vo_range,
        search_results=[result],
        max_candidates=3,
    )

    assert slices
    assert all(slice_.slice_start < 20.0 for slice_ in slices)
    assert all(10.0 <= slice_.window_start <= slice_.window_end <= 12.0 for slice_ in slices)
