from __future__ import annotations

from pathlib import Path

from src.orchestrator import core_editor, models
from src.orchestrator.clip_chooser import ClipChooser, ClipSliceDecision, _clamp_timestamp, _is_outside_range
from src.orchestrator.clip_slices import ClipSlice
from src.orchestrator.director_notes import DirectorNotesBuilder
from src.orchestrator.retrieval.constraints import extract_shot_constraints, validate_candidate
from src.orchestrator.tool_bridge.retrieval_tools import SearchResult


def _candidate(
    *,
    asset_id: str,
    summary: str,
    tags: list[str],
    duration: float,
) -> ClipSlice:
    return ClipSlice(
        asset_id=asset_id,
        source_path=None,
        description=summary,
        slice_start=0.0,
        slice_end=duration,
        window_start=0.0,
        window_end=duration,
        event_summary=summary,
        event_tags=tags,
        event_offset=duration / 2,
        score=1.0,
        reasoning="test candidate",
    )


def test_choose_clip_rejects_short_wrong_mode_candidate_without_gemini() -> None:
    chooser = ClipChooser()
    chooser.client = None

    decision = chooser.choose_clip(
        vo_excerpt="Feixiao demonstrates her ultimate during combat.",
        vo_duration=8.0,
        coverage_note="Show combat footage of the ultimate.",
        beat_intent="Dynamic combat showcase",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
        candidates=[
            _candidate(
                asset_id="menu_clip",
                summary="The screen shows the Traces menu and build stats.",
                tags=["menu", "trace", "ui"],
                duration=1.5,
            )
        ],
    )

    assert decision is None
    assert chooser.last_failure_reason is not None
    assert "duration" in chooser.last_failure_reason


def test_choose_clip_accepts_safety_checked_candidate_without_gemini() -> None:
    chooser = ClipChooser()
    chooser.client = None

    decision = chooser.choose_clip(
        vo_excerpt="Feixiao demonstrates her ultimate during combat.",
        vo_duration=6.0,
        coverage_note="Show combat footage of the ultimate.",
        beat_intent="Dynamic combat showcase",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
        candidates=[
            _candidate(
                asset_id="combat_clip",
                summary="Feixiao uses her ultimate in battle and hits the enemy.",
                tags=["combat", "ultimate"],
                duration=4.0,
            )
        ],
    )

    assert decision is not None
    assert decision.asset_id == "combat_clip"
    assert "Deterministic safety-pass selection" in decision.reasoning


def test_choose_clip_prefer_metadata_skips_gemini_client() -> None:
    chooser = ClipChooser()
    chooser.client = object()

    decision = chooser.choose_clip(
        vo_excerpt="Feixiao demonstrates her ultimate during combat.",
        vo_duration=6.0,
        coverage_note="Show combat footage of the ultimate.",
        beat_intent="Dynamic combat showcase",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
        candidates=[
            _candidate(
                asset_id="combat_clip",
                summary="Feixiao uses her ultimate in battle and hits the enemy.",
                tags=["combat", "ultimate"],
                duration=4.0,
            )
        ],
        prefer_metadata=True,
    )

    assert decision is not None
    assert decision.asset_id == "combat_clip"
    assert "validated metadata candidate" in decision.reasoning


def test_timestamp_range_checks_tolerate_boundary_float_noise() -> None:
    assert not _is_outside_range(85.3900000001, 74.81, 85.39)
    assert _clamp_timestamp(85.3900000001, 74.81, 85.39) == 85.39
    assert _is_outside_range(85.42, 74.81, 85.39)


def test_weapon_attribute_constraint_accepts_gunblade_metadata() -> None:
    constraints = extract_shot_constraints(
        visual_goal=(
            "Focus on Feixiao's unique weapons (battle axe, gunblade) during her attack "
            "animations or a close-up of her character model highlighting them."
        ),
        keywords=["Feixiao weapons", "battle axe", "gunblade"],
    )
    candidate = _candidate(
        asset_id="FEIXIAO TEAM",
        summary="The team lineup shows Feixiao holding her gunblade in an idle pose.",
        tags=["Feixiao", "gunblade", "idle"],
        duration=5.0,
    )
    wrong_candidate = _candidate(
        asset_id="LIGHT CONE",
        summary="The Light Cone menu for Feixiao is displayed.",
        tags=["Feixiao", "light cone", "menu"],
        duration=5.0,
    )

    assert validate_candidate(candidate, constraints).accepted is True
    assert validate_candidate(wrong_candidate, constraints).accepted is False


def test_resolve_shot_clips_records_unresolved_director_note(tmp_path: Path, monkeypatch) -> None:
    beat = models.Beat(
        id="B001",
        segment_ids=["seg-1"],
        visual_intent="Show combat footage",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B001-S01",
        narration_ids=["n1"],
        vo_excerpt="Feixiao uses her ultimate.",
        visual_goal="Show combat footage",
        coverage_note="Show combat footage of the ultimate.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="menu_clip",
                start=0.0,
                end=1.5,
                description="The screen shows the Traces menu and build stats.",
                score=0.8,
                source_path="menu_clip.mp4",
                tags=["menu", "trace", "ui"],
            )
        ],
    )

    class RejectingChooser:
        def __init__(self) -> None:
            self.last_failure_reason = "All candidates rejected by safety gates"

        def choose_clip(self, **kwargs):
            return None

    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=RejectingChooser(),
        debug_dir=tmp_path,
    )

    assert clips == []
    assert len(notes_builder.document.entries) == 1
    entry = notes_builder.document.entries[0]
    assert entry.clip_asset_id == "__UNRESOLVED__"
    assert "hard visual constraints" in entry.reasoning


def test_resolve_shot_clips_uses_fuzzy_only_after_exact_constraints_fail(
    tmp_path: Path, monkeypatch
) -> None:
    beat = models.Beat(
        id="B001",
        segment_ids=["seg-1"],
        visual_intent="Show Feixiao combat footage",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B001-S01",
        narration_ids=["n1"],
        vo_excerpt="Feixiao uses her ultimate.",
        visual_goal="Show Feixiao using her ultimate in combat.",
        coverage_note="Show combat footage of the ultimate.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")
    search_modes: list[str] = []

    def fake_find_clips(*args, **kwargs):
        search_mode = kwargs["constraints"]["search_mode"]
        search_modes.append(search_mode)
        if search_mode == "explicit":
            return [
                SearchResult(
                    asset_id="trace_menu",
                    start=0.0,
                    end=6.0,
                    description="The screen shows a Traces menu and build stats.",
                    score=0.9,
                    source_path="trace_menu.mp4",
                    tags=["menu", "trace", "ui"],
                )
            ]
        return [
            SearchResult(
                asset_id="combat_clip",
                start=10.0,
                end=16.0,
                description="Feixiao uses her ultimate in combat and hits the enemy.",
                score=0.8,
                source_path="combat_clip.mp4",
                tags=["combat", "ultimate", "Feixiao"],
            )
        ]

    monkeypatch.setattr(core_editor.retrieval_tools, "find_clips", fake_find_clips)
    chooser = ClipChooser()
    chooser.client = None

    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert [clip.asset_id for clip in clips] == ["combat_clip"]
    assert search_modes == ["explicit", "fuzzy"]
    debug_text = (tmp_path / "B001" / "B001-S01.json").read_text(encoding="utf-8")
    assert '"fuzzy_fallback_reason"' in debug_text
    assert '"retrieval_mode": "fuzzy"' in debug_text


def test_resolve_shot_clips_does_not_fuzzy_top_up_valid_exact_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    beat = models.Beat(
        id="B002",
        segment_ids=["seg-2"],
        visual_intent="Show Feixiao combat footage",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B002-S01",
        narration_ids=["n2"],
        vo_excerpt="Feixiao uses her ultimate.",
        visual_goal="Show Feixiao using her ultimate in combat.",
        coverage_note="Show combat footage of the ultimate.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")
    search_modes: list[str] = []

    def fake_find_clips(*args, **kwargs):
        search_mode = kwargs["constraints"]["search_mode"]
        search_modes.append(search_mode)
        if search_mode != "explicit":
            raise AssertionError("fuzzy search should not run when exact candidates are usable")
        return [
            SearchResult(
                asset_id="combat_clip",
                start=10.0,
                end=16.0,
                description="Feixiao uses her ultimate in combat and hits the enemy.",
                score=0.8,
                source_path="combat_clip.mp4",
                tags=["combat", "ultimate", "Feixiao"],
            )
        ]

    class MetadataChooser:
        def __init__(self) -> None:
            self.last_failure_reason = None
            self.prefer_metadata_values: list[bool] = []

        def choose_clip(self, **kwargs):
            self.prefer_metadata_values.append(kwargs["prefer_metadata"])
            candidate = kwargs["candidates"][0]
            return ClipSliceDecision(
                asset_id=candidate.asset_id,
                start_time=candidate.slice_start,
                end_time=candidate.slice_end,
                reasoning="metadata accepted",
            )

    monkeypatch.setattr(core_editor.retrieval_tools, "find_clips", fake_find_clips)
    chooser = MetadataChooser()

    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert [clip.asset_id for clip in clips] == ["combat_clip"]
    assert search_modes == ["explicit"]
    assert chooser.prefer_metadata_values == [True]


def test_resolve_shot_clips_returns_multiple_clips_for_long_shot(tmp_path: Path, monkeypatch) -> None:
    beat = models.Beat(
        id="B002",
        segment_ids=["seg-2"],
        visual_intent="Show combat footage",
        pacing="dynamic",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B002-S01",
        narration_ids=["n2"],
        vo_excerpt="Feixiao shows two combat examples for her ultimate.",
        visual_goal="Show two combat examples",
        coverage_note="Use two clips to cover the explanation and payoff.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
        min_clips=1,
        max_clips=2,
    )
    vo_range = models.TimeSpan(start=0.0, duration=10.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="combat_clip_a",
                start=0.0,
                end=5.0,
                description="Feixiao uses her ultimate during battle and hits the enemy.",
                score=0.95,
                source_path="combat_clip_a.mp4",
                tags=["combat", "ultimate"],
            ),
            SearchResult(
                asset_id="combat_clip_b",
                start=5.0,
                end=10.0,
                description="Another combat clip where Feixiao follows up after the ultimate.",
                score=0.9,
                source_path="combat_clip_b.mp4",
                tags=["combat", "ultimate"],
            ),
        ],
    )

    chooser = ClipChooser()
    chooser.client = None

    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert len(clips) == 2
    assert {clip.asset_id for clip in clips} == {"combat_clip_a", "combat_clip_b"}
    assert len(notes_builder.document.entries) == 2


def test_resolve_shot_clips_prefilters_wrong_mode_candidates(tmp_path: Path, monkeypatch) -> None:
    beat = models.Beat(
        id="B003",
        segment_ids=["seg-3"],
        visual_intent="Show combat footage",
        pacing="dynamic",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B003-S01",
        narration_ids=["n3"],
        vo_excerpt="Feixiao demonstrates her ultimate in battle.",
        visual_goal="Show combat footage",
        coverage_note="Avoid menus or build screens here.",
        required_events=["ultimate"],
        keywords=["Feixiao", "ultimate"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="ui_clip",
                start=0.0,
                end=4.0,
                description="The screen shows the Traces menu and build stats.",
                score=0.99,
                source_path="ui_clip.mp4",
                tags=["menu", "trace", "ui"],
            ),
            SearchResult(
                asset_id="combat_clip",
                start=0.0,
                end=4.5,
                description="Feixiao uses her ultimate in battle and hits the enemy.",
                score=0.8,
                source_path="combat_clip.mp4",
                tags=["combat", "ultimate"],
            ),
        ],
    )

    class CapturingChooserWrapper:
        def __init__(self) -> None:
            self.last_failure_reason = None
            self.candidate_asset_ids: list[str] = []

        def choose_clip(self, **kwargs):
            self.candidate_asset_ids = [candidate.asset_id for candidate in kwargs["candidates"]]
            candidate = kwargs["candidates"][0]
            from src.orchestrator.clip_chooser import ClipSliceDecision

            return ClipSliceDecision(
                asset_id=candidate.asset_id,
                start_time=candidate.slice_start,
                end_time=candidate.slice_end,
                reasoning="captured",
            )

    chooser = CapturingChooserWrapper()

    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert len(clips) == 1
    assert set(chooser.candidate_asset_ids) == {"combat_clip"}


def test_resolve_shot_clips_uses_shot_visual_goal_for_mode_prefilter(
    tmp_path: Path, monkeypatch
) -> None:
    beat = models.Beat(
        id="B004",
        segment_ids=["seg-4"],
        visual_intent="Show combat footage later in the beat.",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B004-S01",
        narration_ids=["n4"],
        vo_excerpt="Start by showing the ability details.",
        visual_goal="Show the ability details in a menu screen.",
        coverage_note="Use UI footage for this shot.",
        required_events=[],
        keywords=["ability", "menu"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="ui_clip",
                start=0.0,
                end=8.0,
                description="The screen shows an ability menu with detailed stats.",
                score=0.8,
                source_path="ui_clip.mp4",
                tags=["menu", "ui", "ability"],
            ),
            SearchResult(
                asset_id="combat_clip",
                start=0.0,
                end=8.0,
                description="A character uses an attack in battle and damages the enemy.",
                score=0.9,
                source_path="combat_clip.mp4",
                tags=["combat", "attack", "damage"],
            ),
        ],
    )

    class CapturingChooserWrapper:
        def __init__(self) -> None:
            self.last_failure_reason = None
            self.candidate_asset_ids: list[str] = []

        def choose_clip(self, **kwargs):
            self.candidate_asset_ids = [candidate.asset_id for candidate in kwargs["candidates"]]
            candidate = kwargs["candidates"][0]
            from src.orchestrator.clip_chooser import ClipSliceDecision

            return ClipSliceDecision(
                asset_id=candidate.asset_id,
                start_time=candidate.slice_start,
                end_time=candidate.slice_end,
                reasoning="captured",
            )

    chooser = CapturingChooserWrapper()

    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert len(clips) == 1
    assert set(chooser.candidate_asset_ids) == {"ui_clip"}


def test_resolve_shot_clips_retries_used_assets_when_unused_candidates_fail(
    tmp_path: Path, monkeypatch
) -> None:
    beat = models.Beat(
        id="B004",
        segment_ids=["seg-4"],
        visual_intent="Show Feixiao combat with Ruan Mei support.",
        pacing="dynamic",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B004-S01",
        narration_ids=["n4"],
        vo_excerpt="That goes even faster when you employ Ruan Mei.",
        visual_goal="Show Feixiao's ultimate with Ruan Mei present.",
        coverage_note="Show combat footage with Ruan Mei support.",
        required_events=["Feixiao - Ultimate - Terrasplit", "Ruan Mei in team", "weakness break"],
        keywords=["Feixiao ultimate combat", "Ruan Mei", "weakness break"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=2.5)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="used_valid_combat",
                start=10.0,
                end=17.0,
                description=(
                    "Feixiao - Ultimate - Terrasplit - Feixiao uses her ultimate with "
                    "Ruan Mei in team and triggers a weakness break."
                ),
                score=0.5,
                source_path="used_valid_combat.mp4",
                tags=["combat", "ultimate", "ruanmei", "weakness break"],
            ),
            SearchResult(
                asset_id="unused_bad_combat",
                start=20.0,
                end=20.2,
                description="Feixiao - Ultimate - Terrasplit appears for only a split second.",
                score=10.0,
                source_path="unused_bad_combat.mp4",
                tags=["combat"],
            ),
        ],
    )

    from src.orchestrator.clip_chooser import ClipSliceDecision

    class RetryChooser:
        def __init__(self) -> None:
            self.last_failure_reason = None

        def choose_clip(self, **kwargs):
            asset_ids = {candidate.asset_id for candidate in kwargs["candidates"]}
            if "used_valid_combat" not in asset_ids:
                self.last_failure_reason = "unused candidates rejected"
                return None
            candidate = next(
                candidate
                for candidate in kwargs["candidates"]
                if candidate.asset_id == "used_valid_combat"
            )
            return ClipSliceDecision(
                asset_id=candidate.asset_id,
                start_time=candidate.slice_start,
                end_time=candidate.slice_end,
                reasoning="retry accepted used asset",
            )

    chooser = RetryChooser()
    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets={"used_valid_combat"},
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert len(clips) == 1
    assert clips[0].asset_id == "used_valid_combat"

    debug_path = tmp_path / "B004" / "B004-S01.json"
    assert debug_path.exists()
    assert '"retried_with_used_assets": true' in debug_path.read_text(encoding="utf-8")


def test_resolve_shot_clips_filters_to_explicit_formshift_trace(tmp_path: Path, monkeypatch) -> None:
    beat = models.Beat(
        id="B013",
        segment_ids=["seg"],
        visual_intent="Show Feixiao's Traces menu, highlighting A4.",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B013-S01",
        narration_ids=["n"],
        vo_excerpt="The A4 makes her damage count as Follow Ups.",
        visual_goal=(
            "Display Feixiao's Traces menu, specifically highlighting her A4 bonus ability "
            "(Formshift) and its description."
        ),
        coverage_note="Show Formshift in the Traces menu.",
        required_events=[],
        keywords=["Feixiao Traces menu", "Feixiao A4", "bonus ability 2", "Formshift"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=6.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="VALOROUS MASK",
                start=0.0,
                end=7.0,
                description="Relics menu showing Valorous Mask and Follow-up Attack Crit Damage effects.",
                score=10.0,
                source_path="valorous.mp4",
                tags=["menu", "relic", "ui"],
            ),
            SearchResult(
                asset_id="bonus ability 2",
                start=0.0,
                end=7.0,
                description="Feixiao Traces menu showing Bonus Ability Formshift and Follow-up attacks CRIT DMG.",
                score=1.0,
                source_path="formshift.mp4",
                tags=["menu", "traces", "Feixiao", "Formshift"],
            ),
        ],
    )

    class FirstCandidateChooser:
        def __init__(self) -> None:
            self.last_failure_reason = None
            self.candidate_asset_ids: list[str] = []

        def choose_clip(self, **kwargs):
            from src.orchestrator.clip_chooser import ClipSliceDecision

            self.candidate_asset_ids = [candidate.asset_id for candidate in kwargs["candidates"]]
            candidate = kwargs["candidates"][0]
            return ClipSliceDecision(
                asset_id=candidate.asset_id,
                start_time=candidate.slice_start,
                end_time=candidate.slice_end,
                reasoning="accepted",
            )

    chooser = FirstCandidateChooser()
    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert len(clips) == 1
    assert clips[0].asset_id == "bonus ability 2"
    assert set(chooser.candidate_asset_ids) == {"bonus ability 2"}


def test_resolve_shot_clips_filters_wrong_character_idle(tmp_path: Path, monkeypatch) -> None:
    beat = models.Beat(
        id="B023",
        segment_ids=["seg"],
        visual_intent="Show Feixiao's character model.",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B023-S01",
        narration_ids=["n"],
        vo_excerpt="As for the rest, you go Crit Body.",
        visual_goal="Display Feixiao's character model, performing an idle animation.",
        coverage_note="Introduce Feixiao visually.",
        required_events=[],
        keywords=["Feixiao", "character model", "idle"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=4.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="RUAN MEI",
                start=0.0,
                end=5.0,
                description="Ruan Mei idle animation on the team setup screen.",
                score=3.0,
                source_path="ruan.mp4",
                tags=["idle", "menu"],
            ),
            SearchResult(
                asset_id="idle 4",
                start=0.0,
                end=5.0,
                description="Feixiao performs an idle animation with her weapon visible.",
                score=1.0,
                source_path="idle4.mp4",
                tags=["Feixiao", "idle", "weapon"],
            ),
        ],
    )

    chooser = ClipChooser()
    chooser.client = None
    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert len(clips) == 1
    assert clips[0].asset_id == "idle 4"


def test_resolve_shot_clips_filters_to_topaz_numby_model(tmp_path: Path, monkeypatch) -> None:
    beat = models.Beat(
        id="B033",
        segment_ids=["seg"],
        visual_intent="Show Topaz and Numby.",
        pacing="normal",
        search_queries=[],
    )
    shot = models.ShotInstruction(
        shot_id="B033-S02",
        narration_ids=["n"],
        vo_excerpt="She is quite a classic.",
        visual_goal="Show Topaz & Numby's character model, possibly an idle animation.",
        coverage_note="Show Topaz and Numby clearly.",
        required_events=[],
        keywords=["Topaz & Numby character model", "Topaz idle"],
    )
    vo_range = models.TimeSpan(start=0.0, duration=5.0)
    notes_builder = DirectorNotesBuilder(project_name="test", timeline_name="timeline")

    monkeypatch.setattr(
        core_editor.retrieval_tools,
        "find_clips",
        lambda *args, **kwargs: [
            SearchResult(
                asset_id="ratioSig",
                start=4.5,
                end=9.5,
                description="Feixiao uses Stormborn and attacks the enemy.",
                score=3.0,
                source_path="ratio.mp4",
                tags=["Feixiao", "combat"],
            ),
            SearchResult(
                asset_id="TOPAZ",
                start=0.0,
                end=6.0,
                description="Topaz & Numby are displayed in the team setup menu in an idle pose.",
                score=1.0,
                source_path="topaz.mp4",
                tags=["Topaz & Numby", "idle", "menu"],
            ),
        ],
    )

    chooser = ClipChooser()
    chooser.client = None
    clips = core_editor._resolve_shot_clips(
        beat=beat,
        shot=shot,
        used_assets=set(),
        vo_excerpt=shot.vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        clip_chooser=chooser,
        debug_dir=tmp_path,
    )

    assert len(clips) == 1
    assert clips[0].asset_id == "TOPAZ"
