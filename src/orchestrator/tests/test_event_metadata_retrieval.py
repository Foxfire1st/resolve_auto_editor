from __future__ import annotations

import json
from pathlib import Path

from gateway.vision.event_metadata.ingest import ingest_gemini_json
from src.orchestrator.retrieval.agentic import AgenticRetriever
from src.orchestrator.tool_bridge import retrieval_tools
from src.orchestrator.tool_bridge.retrieval_tools import find_clips
from src.orchestrator.tools.footage import inspect_window, list_assets, search_segments, summarize_asset


def _write_payload(path: Path) -> None:
    payload = {
        "clip_summary": {
            "title": "Combat clip",
            "description": "Feixiao and enemies in battle.",
            "mode": "moc",
            "party": ["Feixiao"],
            "is_combat": True,
        },
        "events": [
            {
                "start_s": 10.0,
                "end_s": 18.5,
                "anchor_s": 12.0,
                "actor": "Feixiao",
                "action_type": "Ultimate",
                "ability_name": "Terrasplit",
                "labels": ["Terrasplit", "Boltsunder Blitz"],
                "details": "Feixiao performs her Terrasplit ultimate.",
                "boundary_evidence": {
                    "start": "ultimate label appears",
                    "end": "final damage resolves",
                },
                "confidence": 0.95,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_metadata(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "combat_clip.mp4"
    video.write_bytes(b"demo")
    gemini_json = tmp_path / "combat_clip_gemini.json"
    database = tmp_path / "event_metadata.db"
    _write_payload(gemini_json)
    ingest_gemini_json(video, gemini_json, database_path=database, profile_name="hsr", clip_duration_s=30.0)
    return video, database


def _ingest_payload(tmp_path: Path, database: Path, stem: str, payload: dict, duration_s: float = 60.0) -> None:
    video = tmp_path / f"{stem}.mp4"
    video.write_bytes(b"demo")
    gemini_json = tmp_path / f"{stem}_gemini.json"
    gemini_json.write_text(json.dumps(payload), encoding="utf-8")
    ingest_gemini_json(video, gemini_json, database_path=database, profile_name="hsr", clip_duration_s=duration_s)


def test_find_clips_reads_event_metadata_without_agentic_services(tmp_path: Path) -> None:
    video, database = _seed_metadata(tmp_path)

    results = find_clips(
        "Feixiao Terrasplit",
        limit=3,
        constraints={"profile": "hsr", "database_path": database, "required_tags": ["combat"]},
    )

    assert len(results) == 1
    result = results[0]
    assert result.asset_id == "combat_clip"
    assert result.start == 10.0
    assert result.end == 18.5
    assert result.source_path == str(video)
    assert result.segment_count == 1
    assert result.events[0].offset == 2.0
    assert "Terrasplit" in result.tags


def test_find_clips_decomposes_narrative_query_into_multiple_retrieval_intents(tmp_path: Path) -> None:
    database = tmp_path / "event_metadata.db"
    _ingest_payload(
        tmp_path,
        database,
        "trace_menu",
        {
            "clip_summary": {"title": "Ultimate trace", "party": ["Feixiao"], "is_combat": False},
            "events": [
                {
                    "start_s": 0.0,
                    "end_s": 9.0,
                    "anchor_s": 3.0,
                    "actor": "Player",
                    "action_type": "UI Interaction",
                    "details": (
                        "The player views Feixiao's Ultimate ability in the Traces menu. "
                        "Terrasplit can ignore Weakness Type to reduce the target's Toughness."
                    ),
                    "confidence": 0.95,
                }
            ],
        },
    )
    _ingest_payload(
        tmp_path,
        database,
        "combat_break",
        {
            "clip_summary": {"title": "Combat break", "party": ["Feixiao", "Ruan Mei"], "is_combat": True},
            "events": [
                {
                    "start_s": 20.0,
                    "end_s": 32.0,
                    "anchor_s": 24.0,
                    "actor": "Feixiao",
                    "action_type": "Ultimate",
                    "ability_name": "Terrasplit",
                    "details": "Feixiao uses Terrasplit, breaks the enemy toughness bar, and deals TOTAL DAMAGE 401253.",
                    "labels": ["Terrasplit", "Break", "TOTAL DAMAGE 401253"],
                    "confidence": 0.95,
                }
            ],
        },
    )
    _ingest_payload(
        tmp_path,
        database,
        "ruan_team",
        {
            "clip_summary": {"title": "Ruan Mei team", "party": ["Feixiao", "Ruan Mei"], "is_combat": False},
            "events": [
                {
                    "start_s": 0.0,
                    "end_s": 15.0,
                    "anchor_s": 5.0,
                    "actor": "Party",
                    "action_type": "Idle",
                    "details": "Team Setup screen showing Feixiao, Topaz, Ruan Mei, and Aventurine.",
                    "labels": ["Feixiao", "Ruan Mei", "Team Setup"],
                    "confidence": 0.95,
                }
            ],
        },
    )

    query = (
        "Focus on Feixiao's Ultimate ability details in the Traces menu, then show combat footage "
        "of her ultimate dealing massive damage and breaking toughness bars, possibly with Ruan Mei in the team. "
        "| Show Feixiao using her Ultimate in combat, clearly demonstrating enemies' toughness bars being "
        "significantly reduced or broken, regardless of weakness type. "
        "| Keywords: Feixiao, ultimate, combat, toughness break, weakness ignore, damage"
    )

    results = find_clips(query, limit=5, constraints={"profile": "hsr", "database_path": database})
    asset_ids = {result.asset_id for result in results}

    assert {"trace_menu", "combat_break", "ruan_team"}.issubset(asset_ids)
    assert next(result for result in results if result.asset_id == "combat_break").end == 32.0


def test_find_clips_surfaces_exact_static_review_match(tmp_path: Path) -> None:
    database = tmp_path / "event_metadata.db"
    _ingest_payload(
        tmp_path,
        database,
        "BAPTISM OF PURE THOUGH",
        {
            "clip_summary": {
                "title": "Feixiao Light Cone Menu - Baptism of Pure Thought",
                "description": "The player views the Light Cone menu for Feixiao.",
                "party": ["Feixiao"],
                "is_combat": False,
            },
            "events": [
                {
                    "start_s": 0.0,
                    "end_s": 0.0,
                    "actor": "Player",
                    "action_type": "UI Interaction",
                    "details": (
                        "The player is viewing the Light Cone menu for Feixiao. "
                        "The equipped Light Cone is 'Baptism of Pure Thought'."
                    ),
                    "confidence": 1.0,
                }
            ],
        },
        duration_s=12.0,
    )

    results = find_clips(
        "Display the 'Baptism of Pure Thought' Light Cone menu",
        limit=3,
        constraints={
            "profile": "hsr",
            "database_path": database,
            "explicit_terms": ["Baptism of Pure Thought"],
        },
    )

    assert results
    assert results[0].asset_id == "BAPTISM OF PURE THOUGH"
    assert results[0].start == 0.0
    assert results[0].end == 12.0
    assert "#explicit-review" in results[0].tags


def test_find_clips_searches_stored_clip_summary_for_explicit_terms(tmp_path: Path) -> None:
    database = tmp_path / "event_metadata.db"
    _ingest_payload(
        tmp_path,
        database,
        "light_cone_menu",
        {
            "clip_summary": {
                "title": "Baptism of Pure Thought Light Cone screen",
                "description": "Static character equipment capture for Feixiao.",
                "party": ["Feixiao"],
                "is_combat": False,
            },
            "events": [
                {
                    "start_s": 0.0,
                    "end_s": 0.0,
                    "actor": "Player",
                    "action_type": "UI Interaction",
                    "details": "The player views a Light Cone menu.",
                    "confidence": 1.0,
                }
            ],
        },
        duration_s=9.0,
    )

    results = find_clips(
        "Display the 'Baptism of Pure Thought' Light Cone menu",
        limit=3,
        constraints={
            "profile": "hsr",
            "database_path": database,
            "explicit_terms": ["Baptism of Pure Thought"],
        },
    )

    assert results
    assert results[0].asset_id == "light_cone_menu"
    assert "Baptism of Pure Thought" in results[0].description
    assert results[0].end == 9.0


def test_find_clips_searches_manifest_for_explicit_terms(tmp_path: Path) -> None:
    database = tmp_path / "event_metadata.db"
    manifest = tmp_path / "manifest.jsonl"
    video = tmp_path / "formshift.mp4"
    video.write_bytes(b"demo")
    manifest.write_text(
        json.dumps(
            {
                "asset_id": "bonus ability 2",
                "summary": "Feixiao Formshift trace details in the Traces menu.",
                "tags": ["traces", "ui"],
                "duration": 14.0,
                "source_path": str(video),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    results = find_clips(
        "Show the Formshift trace in the Traces menu",
        limit=3,
        constraints={
            "profile": "hsr",
            "database_path": database,
            "manifest_path": manifest,
            "explicit_terms": ["Formshift"],
        },
    )

    assert results
    assert results[0].asset_id == "bonus ability 2"
    assert results[0].start == 0.0
    assert results[0].end == 14.0
    assert "#explicit-manifest" in results[0].tags


def test_find_clips_skips_broad_search_when_explicit_lookup_hits(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "event_metadata.db"
    manifest = tmp_path / "manifest.jsonl"
    video = tmp_path / "formshift.mp4"
    video.write_bytes(b"demo")
    manifest.write_text(
        json.dumps(
            {
                "asset_id": "bonus ability 2",
                "summary": "Feixiao Formshift trace details in the Traces menu.",
                "tags": ["traces", "ui"],
                "duration": 14.0,
                "source_path": str(video),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    search_calls: list[str] = []

    def fake_search_events(query: str = "", **kwargs):
        search_calls.append(query)
        return []

    monkeypatch.setattr(retrieval_tools, "search_events", fake_search_events)

    results = retrieval_tools.find_clips(
        "Show the Formshift trace in the Traces menu | Keywords: Feixiao, Formshift, trace menu",
        limit=5,
        constraints={
            "profile": "hsr",
            "database_path": database,
            "manifest_path": manifest,
            "explicit_terms": ["Formshift"],
        },
    )

    assert results
    assert results[0].asset_id == "bonus ability 2"
    assert search_calls == []


def test_find_clips_explicit_mode_never_falls_back_to_broad_search(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "event_metadata.db"
    search_calls: list[str] = []

    def fake_search_events(query: str = "", **kwargs):
        search_calls.append(query)
        return []

    monkeypatch.setattr(retrieval_tools, "search_events", fake_search_events)

    results = retrieval_tools.find_clips(
        "Show Feixiao holding her weapon in combat",
        limit=5,
        constraints={
            "profile": "hsr",
            "database_path": database,
            "explicit_terms": ["Feixiao weapon"],
            "search_mode": "explicit",
        },
    )

    assert results == []
    assert search_calls == []


def test_agentic_retriever_is_event_metadata_compatibility_wrapper(tmp_path: Path) -> None:
    _video, database = _seed_metadata(tmp_path)

    result = AgenticRetriever().retrieve(
        "Terrasplit",
        search_kwargs={"profile_name": "hsr", "database_path": database},
    )

    assert result.selection is not None
    assert result.selection.clip.video_id == "combat_clip"
    assert result.selection.clip.start_time == 10.0
    assert len(result.candidates) == 1
    assert result.candidates[0].segment_ids == result.selection.clip.segment_ids


def test_footage_tools_read_event_metadata_database(tmp_path: Path) -> None:
    _video, database = _seed_metadata(tmp_path)
    constraints = {"profile": "hsr", "database_path": database}

    search_output = search_segments("Terrasplit", constraints)
    assert search_output["found_match"] is True
    assert search_output["selected_clip"]["video_id"] == "combat_clip"

    assets = list_assets("hsr", page=1, page_size=10, database_path=str(database))
    summary = summarize_asset("combat_clip", profile="hsr", database_path=str(database))
    window = inspect_window("combat_clip", 9.0, 19.0, profile="hsr", database_path=str(database))

    assert assets == [
        {
            "asset_id": "combat_clip",
            "duration": 30.0,
            "source_path": str(tmp_path / "combat_clip.mp4"),
        }
    ]
    assert summary["asset_id"] == "combat_clip"
    assert summary["segment_count"] == 1
    assert "Terrasplit ultimate." in window["segments"][0]["caption"]
