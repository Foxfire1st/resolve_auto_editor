"""Profile-aware helpers for loading prompts, scripts, and resources."""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Any, Callable

PROFILES_ROOT_ENV = "AUTO_EDITOR_PROFILES_ROOT"
ACTIVE_PROFILE_ENV = "AUTO_EDITOR_PROFILE"
SETTINGS_PATH = Path("settings.json")


@lru_cache(maxsize=1)
def load_global_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"settings.json contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("settings.json must contain a JSON object at the top level.")
    return data


@dataclass(frozen=True)
class ProfilePaths:
    name: str
    root: Path
    data: Path
    prompts: Path
    resources: Path
    scripts: Path


def get_profiles_root() -> Path:
    configured = os.getenv(PROFILES_ROOT_ENV)
    base = Path(configured).expanduser() if configured else Path("profiles")
    return base.resolve()


def profile_exists(profile_name: str) -> bool:
    try:
        return get_profile_paths(profile_name, missing_ok=True).root.exists()
    except ValueError:
        return False


def get_profile_paths(profile_name: str, *, missing_ok: bool = False) -> ProfilePaths:
    name = (profile_name or "").strip()
    if not name:
        raise ValueError("profile_name must be a non-empty string")
    root = (get_profiles_root() / name).resolve()
    if not root.exists() and not missing_ok:
        raise FileNotFoundError(
            f"Profile '{name}' not found at {root}. Set {PROFILES_ROOT_ENV} to override the profiles root."
        )
    return ProfilePaths(
        name=name,
        root=root,
        data=root / "data",
        prompts=root / "prompts",
        resources=root / "resources",
        scripts=root / "scripts",
    )


def ensure_profile_dirs(profile_name: str, directories: Iterable[str] | None = None) -> ProfilePaths:
    paths = get_profile_paths(profile_name, missing_ok=True)
    for entry in directories or ("data", "prompts", "resources", "scripts"):
        (paths.root / entry).mkdir(parents=True, exist_ok=True)
    return paths


def default_profile_name() -> str | None:
    env_value = os.getenv(ACTIVE_PROFILE_ENV, "").strip()
    if env_value:
        return env_value
    settings = load_global_settings()
    configured = settings.get("active_profile") if isinstance(settings, dict) else None
    if isinstance(configured, str):
        configured = configured.strip()
        if configured:
            return configured
    return None


@lru_cache(maxsize=32)
def load_profile_manifest(profile_name: str) -> dict[str, Any]:
    paths = get_profile_paths(profile_name)
    manifest_path = paths.root / "profile.json"
    if not manifest_path.exists():
        return {}
    with manifest_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_profile_file(profile_name: str, relative_path: str | Path) -> Path:
    paths = get_profile_paths(profile_name)
    target = (paths.root / relative_path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"Profile '{profile_name}' file not found: {relative_path}")
    return target


def load_prompt_text(profile_name: str | None, key: str) -> str | None:
    if not profile_name:
        return None
    manifest = load_profile_manifest(profile_name)
    prompt_map = manifest.get("prompts") or {}
    relative = prompt_map.get(key)
    if not relative:
        return None
    try:
        prompt_path = resolve_profile_file(profile_name, relative)
    except FileNotFoundError:
        return None
    return prompt_path.read_text(encoding="utf-8").strip()


def get_manifest_node(profile_name: str | None, *keys: str) -> Any:
    if not profile_name:
        return None
    node: Any = load_profile_manifest(profile_name)
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node


@lru_cache(maxsize=64)
def _load_profile_module(profile_name: str, relative_path: str) -> Any:
    script_path = resolve_profile_file(profile_name, relative_path)
    safe_relative = relative_path.replace("/", "_").replace("\\", "_")
    module_name = f"profile_{profile_name}_{safe_relative}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load profile script at {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_profile_function(profile_name: str | None, relative_path: str, function_name: str) -> Callable | None:
    if not profile_name:
        return None
    try:
        module = _load_profile_module(profile_name, relative_path)
    except (FileNotFoundError, ImportError):
        return None
    return getattr(module, function_name, None)


def execute_profile_function(
    profile_name: str | None,
    relative_path: str,
    function_name: str,
    *args,
    **kwargs,
):
    func = load_profile_function(profile_name, relative_path, function_name)
    if func is None:
        raise RuntimeError(
            f"Profile '{profile_name}' does not expose {function_name} in {relative_path}."
        )
    return func(*args, **kwargs)
