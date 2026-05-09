"""Command-line interface for the Gemini ingest prototype."""

from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import analyze_clip, collect_batch_requests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze a clip or folder of clips with Gemini 2.5 Flash.")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--video-path",
        type=Path,
        help="Path to the source video clip.",
    )
    input_group.add_argument(
        "--footage-root",
        type=Path,
        help=(
            "Process every clip inside <root>/<regular> and <root>/<slowdown>, "
            "saving JSON into each folder's meta directory."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        help="Optional path to a text file containing a custom prompt.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output path for the JSON response.",
    )
    parser.add_argument(
        "--model",
        type=str,
        help="Override the default Gemini model name.",
    )
    parser.add_argument(
        "--slowdown",
        type=float,
        help="Slowdown factor applied before upload (e.g., 5.0 makes clips 5x longer).",
    )
    parser.add_argument(
        "--media-resolution",
        type=str,
        help="Optional Gemini media resolution override (low, medium, high).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        help="Override the Gemini temperature (creativity) setting.",
    )
    parser.add_argument(
        "--thinking-level",
        choices=("low", "high"),
        help="Set the Gemini thinking level (if supported by the model).",
    )
    parser.add_argument(
        "--skip-roster-pass",
        action="store_true",
        help="Skip the preliminary roster detection frame analysis.",
    )
    parser.add_argument(
        "--enable-google-search",
        action="store_true",
        help="Enable Gemini Google Search grounding for this run.",
    )
    parser.add_argument(
        "--enable-structured-output",
        action="store_true",
        help="Request structured output enforcement via JSON schema.",
    )
    parser.add_argument(
        "--keep-upload",
        action="store_true",
        help="Do not delete the uploaded file from Gemini storage after processing.",
    )
    parser.add_argument(
        "--regular-folder-name",
        default="regular",
        help="Subfolder (within footage root) for real-time clips (default: regular).",
    )
    parser.add_argument(
        "--regular-folder-slowdown",
        type=float,
        default=1.0,
        help="Slowdown factor applied to the regular folder during batch mode.",
    )
    parser.add_argument(
        "--slowdown-folder-name",
        default="slowdown",
        help="Subfolder (within footage root) for combat/slow clips (default: slowdown).",
    )
    parser.add_argument(
        "--slowdown-folder-factor",
        type=float,
        default=5.0,
        help="Slowdown factor applied to the slowdown folder during batch mode.",
    )
    parser.add_argument(
        "--batch-meta-folder-name",
        default="meta",
        help="Meta subdirectory where batch outputs are written (default: meta).",
    )
    parser.add_argument(
        "--batch-video-extensions",
        default=".mp4,.mov,.mkv,.avi,.m4v,.webm",
        help="Comma-separated list of extensions to scan in batch mode.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-run Gemini even if a JSON already exists (batch mode).",
    )
    parser.add_argument(
        "--batch-clip",
        action="append",
        dest="batch_clips",
        help=(
            "Limit batch processing to specific clip names or relative paths. "
            "Example: --batch-clip slowdown/mozerobinAS.mp4"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    prompt_text = None
    if args.prompt_file:
        prompt_text = args.prompt_file.read_text(encoding="utf-8")

    if args.footage_root and args.output:
        parser.error("--output cannot be combined with --footage-root; files are written to each meta folder.")

    common_kwargs = dict(
        prompt=prompt_text,
        model=args.model,
        media_resolution=args.media_resolution,
        skip_roster_pass=args.skip_roster_pass,
        enable_google_search=args.enable_google_search,
        temperature=args.temperature,
        thinking_level=args.thinking_level,
        enable_structured_output=args.enable_structured_output,
        keep_upload=args.keep_upload,
    )

    if args.video_path:
        _, output_path = analyze_clip(
            args.video_path,
            output_path=args.output,
            slowdown=args.slowdown,
            **common_kwargs,
        )
        print("Gemini analysis complete.")
        print(f"Saved output to {output_path}")
        return

    folder_config: dict[str, float] = {}
    if args.regular_folder_name:
        folder_config[args.regular_folder_name] = args.regular_folder_slowdown or 1.0
    if args.slowdown_folder_name:
        folder_config[args.slowdown_folder_name] = args.slowdown_folder_factor or 1.0
    extensions = tuple(
        part.strip()
        for part in args.batch_video_extensions.split(",")
        if part.strip()
    )
    clip_filter = tuple(args.batch_clips) if args.batch_clips else None
    batch_requests = collect_batch_requests(
        args.footage_root,
        folder_slow_map=folder_config,
        meta_folder_name=args.batch_meta_folder_name,
        allowed_extensions=extensions,
        overwrite=args.overwrite,
        clip_name_filter=clip_filter,
    )

    if not batch_requests:
        print("No footage files matched the batch criteria.")
        return

    total = len(batch_requests)
    print(f"[Gemini][batch] Processing {total} clips from {args.footage_root} ...")
    for idx, request in enumerate(batch_requests, start=1):
        print(
            f"[Gemini][batch] ({idx}/{total}) {request.video_path.name} "
            f"-> {request.output_path}"
        )
        analyze_clip(
            request.video_path,
            output_path=request.output_path,
            slowdown=request.slowdown,
            **common_kwargs,
        )
    print("Gemini batch ingest complete.")


if __name__ == "__main__":
    main()
