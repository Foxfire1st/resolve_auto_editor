"""Configuration helpers for the Gemini ingest pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class GeminiSettings:
    """Runtime configuration for Gemini calls."""

    api_key: str
    model: str = "gemini-2.5-flash"
    output_dir: Path = Path("src/gateway/vision/gemini_ingest/samples")
    default_slowdown_factor: float = 1.0
    media_resolution: str | None = None
    enable_google_search: bool = False
    temperature: float | None = None
    thinking_level: str | None = None
    enable_structured_output: bool = True
    skip_roster_pass: bool = False
    response_retry_attempts: int = 2


@lru_cache(maxsize=1)
def get_settings() -> GeminiSettings:
    """Load settings from environment variables."""
    api_key = (
        os.getenv("GOOGLE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_GEMINI_API_KEY")
        or os.getenv("gemini_api_key")
    )
    if not api_key:
        raise RuntimeError(
            "Missing Google Gemini API key. Set GOOGLE_API_KEY in .env."
    )
    model = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")
    output_dir = Path(os.getenv("GEMINI_OUTPUT_DIR", "src/gateway/vision/gemini_ingest/samples"))
    slowdown_factor = float(os.getenv("GEMINI_SLOWDOWN_FACTOR", "1.0"))
    media_resolution = os.getenv("GEMINI_MEDIA_RESOLUTION")
    temperature = os.getenv("GEMINI_TEMPERATURE")
    temperature_value = None
    if temperature:
        try:
            temperature_value = float(temperature)
        except ValueError:
            raise RuntimeError("GEMINI_TEMPERATURE must be a float.")
    thinking_level = os.getenv("GEMINI_THINKING_LEVEL")
    search_enabled = os.getenv("GEMINI_ENABLE_GOOGLE_SEARCH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    structured_flag = os.getenv("GEMINI_ENABLE_STRUCTURED_OUTPUT")
    if structured_flag is None:
        structured_enabled = True
    else:
        structured_enabled = structured_flag.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    skip_roster = os.getenv("GEMINI_SKIP_ROSTER_PASS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        response_retries = int(os.getenv("GEMINI_RESPONSE_RETRIES", "2"))
    except ValueError as exc:
        raise RuntimeError("GEMINI_RESPONSE_RETRIES must be an integer.") from exc
    return GeminiSettings(
        api_key=api_key,
        model=model,
        output_dir=output_dir,
        default_slowdown_factor=slowdown_factor,
        media_resolution=media_resolution,
        enable_google_search=search_enabled,
        temperature=temperature_value,
        thinking_level=thinking_level,
        enable_structured_output=structured_enabled,
        skip_roster_pass=skip_roster,
        response_retry_attempts=max(response_retries, 1),
    )
