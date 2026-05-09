from __future__ import annotations

import json
from pathlib import Path

from gateway.vision.event_metadata.cli import main
from gateway.vision.event_metadata.search import search_events


def _write_payload(path: Path) -> None:
    payload = {
        "clip_summary": {
            "title": "Combat clip",
            "party": ["Feixiao"],
            "is_combat": True,
        },
        "events": [
            {
                "start_s": 3.0,
                "end_s": 9.0,
                "anchor_s": 4.5,
                "actor": "Feixiao",
                "action_type": "Ultimate",
                "ability_name": "Terrasplit",
                "labels": ["Terrasplit"],
                "details": "Feixiao performs her ultimate.",
                "boundary_evidence": {"start": "label appears", "end": "final hit lands"},
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_batch_ingest_and_describe_commands(tmp_path: Path, capsys) -> None:
    footage_root = tmp_path / "footage"
    regular = footage_root / "regular"
    meta = regular / "meta"
    meta.mkdir(parents=True)
    video = regular / "combat_clip.mp4"
    video.write_bytes(b"demo")
    _write_payload(meta / "combat_clip_gemini.json")
    database = tmp_path / "event_metadata.db"

    assert main([
        "batch-ingest",
        "--footage-root",
        str(footage_root),
        "--folders",
        "regular",
        "--profile",
        "hsr",
        "--database",
        str(database),
    ]) == 0
    assert search_events("Terrasplit", profile_name="hsr", database_path=database)

    assert main([
        "describe",
        "--profile",
        "hsr",
        "--database",
        str(database),
        "--contains",
        "combat",
    ]) == 0
    output = capsys.readouterr().out
    assert "video=combat_clip" in output
    assert "Feixiao performs her ultimate." in output
