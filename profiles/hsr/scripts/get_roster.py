"""Load Honkai: Star Rail roster metadata from profile resources."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from src.gateway.profiles import get_profile_paths

VALID_ABILITY_TYPES = {"Basic ATK", "Skill", "Ultimate", "Talent", "Technique"}
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_PROFILE = "hsr"
_DATA_ROOT_ENV = "HSR_DATA_ROOT"


def get_character_ability_lookup(
    candidate_names: Iterable[str],
    *,
    profile_name: str | None = None,
) -> dict[str, dict[str, dict[str, str]]]:
    """Return ability metadata for the provided character names."""

    profile = _normalize_profile(profile_name)
    name_to_ids = _character_name_index(profile)
    abilities = _ability_catalog(profile)
    display_names = _character_display_names(profile)
    roster: dict[str, dict[str, dict[str, str]]] = {}
    for raw_name in candidate_names:
        if not raw_name:
            continue
        candidates = name_to_ids.get(_normalize(raw_name), [])
        if not candidates:
            continue
        matched_name = raw_name
        ability_map: dict[str, dict[str, str]] | None = None
        matched_id: str | None = None
        for char_id in candidates:
            ability_map = abilities.get(char_id)
            if ability_map:
                matched_id = char_id
                break
        if not ability_map or not matched_id:
            continue
        canonical = display_names.get(matched_id, matched_name)
        roster[canonical] = {slot: data.copy() for slot, data in ability_map.items()}
    return roster


def get_enemy_ability_lookup(
    enemy_names: Iterable[str],
    *,
    profile_name: str | None = None,
) -> dict[str, dict[str, object]]:
    """Return ability/weakness metadata for visible enemies."""

    profile = _normalize_profile(profile_name)
    records = _monster_records(profile)
    details = _monster_details(profile)
    name_index = _monster_name_index(profile)
    mapped: dict[str, dict[str, object]] = {}
    for raw_name in enemy_names:
        if not raw_name:
            continue
        candidates = name_index.get(_normalize(raw_name), [])
        if not candidates:
            continue
        chosen_id = None
        record = None
        for monster_id in candidates:
            record = records.get(monster_id)
            if record:
                chosen_id = monster_id
                break
        if not record or not chosen_id:
            continue
        detail = details.get(chosen_id)
        canonical = record.get("name") or raw_name
        weakness = record.get("weaknesses") or []
        description = record.get("description", "")
        skills = _extract_monster_skills(detail)
        mapped[canonical] = {
            "description": description,
            "weaknesses": weakness,
            "skills": skills,
        }
    return mapped


@lru_cache(maxsize=4)
def _character_display_names(profile_name: str) -> dict[str, str]:
    records = _game_data(profile_name).get("characters", {})
    return {
        cid: record.get("name", cid)
        for cid, record in records.items()
        if isinstance(record, dict)
    }


@lru_cache(maxsize=4)
def _character_name_index(profile_name: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for cid, name in _character_display_names(profile_name).items():
        index.setdefault(_normalize(name), []).append(cid)
    return index


@lru_cache(maxsize=4)
def _ability_catalog(profile_name: str) -> dict[str, dict[str, dict[str, str]]]:
    data = _load_json("character_skills.json", profile_name)
    catalog: dict[str, dict[str, dict[str, str]]] = {}
    for ability_id, payload in data.items():
        if not isinstance(payload, dict):
            continue
        char_id = ability_id[:4]
        ability_type = (payload.get("type_text") or "").strip()
        if ability_type not in VALID_ABILITY_TYPES:
            continue
        name = (payload.get("name") or "").strip()
        description = (payload.get("simple_desc") or payload.get("desc") or "").strip()
        if not (name or description):
            continue
        catalog.setdefault(char_id, {})[ability_type] = {
            "name": name,
            "description": description,
        }
    return catalog


@lru_cache(maxsize=4)
def _game_data(profile_name: str) -> dict:
    try:
        return _load_json("game_data.json", profile_name)
    except RuntimeError:
        data = _load_json("characters.json", profile_name)
        if "characters" not in data:
            data = {"characters": data}
        return data


def _load_json(filename: str, profile_name: str) -> dict:
    for base in _data_directories(profile_name):
        candidate = base / filename
        if candidate.exists():
            with candidate.open("r", encoding="utf-8") as handle:
                return json.load(handle)
    raise RuntimeError(f"HSR data file {filename} not found in any known directory.")


def _data_directories(profile_name: str) -> list[Path]:
    """Return directories to probe for data, preferring profile resources."""

    dirs: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        if resolved.exists():
            dirs.append(resolved)
            seen.add(resolved)

    env_override = os.getenv(_DATA_ROOT_ENV)
    if env_override:
        _add(Path(env_override).expanduser())

    try:
        profile_paths = get_profile_paths(profile_name, missing_ok=True)
    except ValueError:
        profile_paths = None

    if profile_paths:
        _add(profile_paths.resources)
        _add(profile_paths.data)

    legacy_assets = _repo_root() / "assets" / profile_name
    _add(legacy_assets)

    return dirs


def _normalize_profile(profile_name: str | None) -> str:
    name = (profile_name or _DEFAULT_PROFILE).strip()
    return name or _DEFAULT_PROFILE


def _normalize(value: str) -> str:
    return _NORMALIZE_RE.sub("", value.lower())


@lru_cache(maxsize=4)
def _monster_records(profile_name: str) -> dict[str, dict]:
    try:
        data = _load_json("monsters.json", profile_name)
    except RuntimeError:
        return {}
    records: dict[str, dict] = {}
    if isinstance(data, list):
        for entry in data:
            monster_id = str(entry.get("id"))
            if monster_id:
                records[monster_id] = entry
    return records


@lru_cache(maxsize=4)
def _monster_details(profile_name: str) -> dict[str, dict]:
    try:
        data = _load_json("monster_details.json", profile_name)
    except RuntimeError:
        return {}
    return {str(key): value for key, value in data.items()}


@lru_cache(maxsize=4)
def _monster_name_index(profile_name: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for monster_id, record in _monster_records(profile_name).items():
        names = [record.get("name", "")]
        extra = record.get("names")
        if isinstance(extra, dict):
            names.extend(extra.values())
        for name in names:
            norm = _normalize(name)
            if norm:
                index.setdefault(norm, []).append(monster_id)
    return index


def _extract_monster_skills(detail: dict | None) -> list[dict[str, str]]:
    if not detail:
        return []
    seen: set[tuple[str, str]] = set()
    skills: list[dict[str, str]] = []
    for monster_type in detail.get("monster_types") or []:
        for skill in monster_type.get("skills") or []:
            name = (skill.get("name") or "").strip()
            description = (skill.get("desc") or "").strip()
            if not (name or description):
                continue
            key = (name, description)
            if key in seen:
                continue
            seen.add(key)
            entry = {"name": name, "description": description}
            if skill.get("damage_type"):
                entry["damage_type"] = skill["damage_type"]
            skills.append(entry)
    return skills


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "profiles").exists() or (parent / "assets").exists():
            return parent
    return current.parents[-1]
