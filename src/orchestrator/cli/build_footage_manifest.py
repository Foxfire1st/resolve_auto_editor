"""CLI to export ingest summaries into a Gemini-ready manifest file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.orchestrator import beat_manager
from src.orchestrator.retrieval import footage_manifest

DEFAULT_PROJECT_ROOT = Path("samples/feixiao test edit")
DEFAULT_PROJECT_SETTINGS = DEFAULT_PROJECT_ROOT / "project.settings.json"
DEFAULT_FOOTAGE_ROOT = DEFAULT_PROJECT_ROOT / "footage"
DEFAULT_OUTPUT = DEFAULT_PROJECT_ROOT / "outputs" / "footage_manifest.ndjson"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export footage summaries into a reusable manifest for Gemini Files uploads.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--project-settings",
        type=Path,
        default=DEFAULT_PROJECT_SETTINGS,
        help="Path to project.settings.json used to infer media roots.",
    )
    parser.add_argument(
        "--footage-root",
        type=Path,
        action="append",
        help="Optional footage root(s) to scan. Use multiple times to override settings.",
    )
    return parser


def _resolve_roots(args: argparse.Namespace) -> list[Path]:
    settings = beat_manager.load_project_settings(args.project_settings)
    settings_roots = beat_manager.extract_footage_roots_from_settings(
        settings,
        base_dir=args.project_settings.parent if args.project_settings else None,
    )

    if args.footage_root:
        return args.footage_root
    if settings_roots:
        return settings_roots
    return [DEFAULT_FOOTAGE_ROOT]


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    footage_roots = _resolve_roots(args)

    try:
        path = footage_manifest.build_manifest(
            footage_roots=footage_roots,
            output_path=args.output,
        )
    except RuntimeError as exc:
        print(f"[build_footage_manifest] Failed: {exc}")
        sys.exit(1)

    print(f"[build_footage_manifest] Wrote manifest with {path.stat().st_size} bytes -> {path}")


if __name__ == "__main__":
    main()
