"""Validate cached Gemini Files handles before running automations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.orchestrator.retrieval import attachment_state

DEFAULT_PROJECT_ROOT = Path("samples/feixiao test edit")
DEFAULT_STATE_FILE = DEFAULT_PROJECT_ROOT / "outputs" / attachment_state.DEFAULT_STATE_FILENAME


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Gemini Files handles recorded in an attachment state file.",
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument(
        "--attachment",
        action="append",
        help="Attachment name(s) to check (default: all recorded attachments).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    state = attachment_state.list_attachments(args.state_file)
    if not state:
        print(f"[check_gemini_handles] No attachments recorded in {args.state_file}")
        sys.exit(0)

    targets = set(args.attachment) if args.attachment else set(state.keys())
    missing = [name for name in targets if name not in state]
    if missing:
        print(f"[check_gemini_handles] Attachment(s) not found in state: {', '.join(sorted(missing))}")
        sys.exit(1)

    exit_code = 0
    for name in sorted(targets):
        entry = state[name]
        status = attachment_state.attachment_status(entry)
        detail = (
            f"gemini={entry.get('gemini_name')} expires={entry.get('expire_time') or '≈48h window'}"
        )
        print(f"[check_gemini_handles] {name}: {status} ({detail})")
        if status != "valid":
            exit_code = 2

    sys.exit(exit_code)


if __name__ == "__main__":
    main()