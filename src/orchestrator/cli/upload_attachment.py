"""Upload arbitrary files to Gemini and record the handle state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.orchestrator.clients import gemini_files
from src.orchestrator.retrieval import attachment_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload a file to Gemini Files and record state.")
    parser.add_argument("--name", required=True, help="Logical attachment name (e.g., footage_manifest)")
    parser.add_argument("--file", type=Path, required=True, help="Path to the file to upload")
    parser.add_argument(
        "--handle-path",
        type=Path,
        help="Optional handle metadata path (defaults to <file>.handle.json)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        required=True,
        help="Attachment state JSON to update",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="Reuse an existing handle if it matches the file and is not expired.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    file_path = args.file
    if not file_path.exists():
        print(f"[upload_attachment] File not found: {file_path}")
        sys.exit(1)

    handle_path = args.handle_path or file_path.with_suffix(file_path.suffix + ".handle.json")

    client = gemini_files.GeminiFilesClient()
    handle = None
    if args.reuse:
        cached = gemini_files.load_handle(handle_path)
        if cached and gemini_files.handle_matches_file(cached, file_path):
            handle = cached
            print(
                f"[upload_attachment] Reusing handle {cached.gemini_name} for {args.name}"
            )

    if handle is None:
        handle = client.upload(file_path)
        gemini_files.save_handle(handle, handle_path)
        print(
            f"[upload_attachment] Uploaded {file_path.name} ({handle.size_bytes} bytes) -> {handle.gemini_name}"
        )

    attachment_state.record_attachment(
        state_path=args.state_file,
        attachment_name=args.name,
        handle=handle,
        handle_path=handle_path,
    )
    print(f"[upload_attachment] Recorded attachment '{args.name}' in {args.state_file}")


if __name__ == "__main__":
    main()