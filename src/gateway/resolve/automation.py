"""Resolve automation logic running inside the Resolve process."""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from typing import Any

from src.gateway.resolve.bridge import ResolveBridge, ResolveNotAvailableError


class ResolveAutomation:
    """Facade around ``ResolveBridge`` exposing high-level automation methods."""

    def __init__(self, project_name: str) -> None:
        self._bridge = ResolveBridge(project_name=project_name)

    def import_fcpxml(self, fcpxml_path: Path, *, timeline_name: str | None = None) -> str:
        return self._bridge.import_fcpxml(fcpxml_path, timeline_name=timeline_name)

    def render(self, destination: Path, *, preset: str = "YouTube 1080p", format_: str = "mp4", codec: str = "H.264") -> Path:
        return self._bridge.render_timeline(destination, preset=preset, format_=format_, codec=codec)

    def export_timeline_archive(self, destination: Path) -> Path:
        return self._bridge.export_timeline_archive(destination)

    def capture_timeline_snapshot(self, destination: Path | None = None) -> dict[str, Any]:
        """Capture the active timeline and optionally persist it to disk."""

        return self._bridge.capture_timeline_snapshot(destination)

    def rebuild_timeline_from_snapshot(
        self,
        snapshot_path: Path | dict[str, Any],
        *,
        timeline_name: str | None = None,
        wipe_media_pool: bool = True,
        delete_timelines: bool = True,
    ) -> str:
        """Recreate a timeline from a captured snapshot."""

        return self._bridge.rebuild_timeline_from_snapshot(
            snapshot_path,
            timeline_name=timeline_name,
            wipe_media_pool=wipe_media_pool,
            delete_timelines=delete_timelines,
        )

    def wipe_media_pool(self, *, delete_timelines: bool = True) -> None:
        self._bridge.wipe_media_pool(delete_timelines=delete_timelines)

    def relink_timeline_clips(
        self,
        *,
        notes_path: Path | None = None,
        report_path: Path | None = None,
    ) -> dict[str, Any]:
        """Iterate over the active timeline, refresh clips from the media pool, and log metadata."""

        return self._bridge.relink_timeline_clips(notes_path=notes_path, report_path=report_path)

    def import_render_export(
        self,
        *,
        fcpxml_path: Path,
        render_path: Path,
        archive_path: Path,
        timeline_name: str | None = None,
        preset: str = "YouTube 1080p",
        format_: str = "mp4",
        codec: str = "H.264",
    ) -> dict[str, Path | str]:
        timeline = self.import_fcpxml(fcpxml_path, timeline_name=timeline_name)
        rendered = self.render(render_path, preset=preset, format_=format_, codec=codec)
        archive = self.export_timeline_archive(archive_path)
        return {"timeline_name": timeline, "render_path": rendered, "archive_path": archive}

    def get_project_info(self) -> dict[str, Any]:
        """Retrieve metadata about the active project."""
        project = self._bridge._ensure_project_loaded()
        return {
            "name": project.GetName(),
            "timeline_count": project.GetTimelineCount(),
            "frame_rate": project.GetSetting("timelineFrameRate"),
            "resolution_width": project.GetSetting("timelineResolutionWidth"),
            "resolution_height": project.GetSetting("timelineResolutionHeight"),
        }

    def list_timelines(self) -> list[dict[str, Any]]:
        """List all timelines in the project."""
        project = self._bridge._ensure_project_loaded()
        timelines = []
        for i in range(1, project.GetTimelineCount() + 1):
            timeline = project.GetTimelineByIndex(i)
            if timeline:
                timelines.append({
                    "index": i,
                    "name": timeline.GetName(),
                    "start_timecode": timeline.GetStartTimecode(),
                    "duration": timeline.GetEndFrame() - timeline.GetStartFrame()
                })
        return timelines


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve Automation CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-fcpxml", help="Import FCPXML into Resolve")
    import_parser.add_argument("--fcpxml", type=str, required=True, help="Path to FCPXML file")
    import_parser.add_argument("--timeline", type=str, default="Imported Timeline", help="Name for the new timeline")
    import_parser.add_argument("--project", type=str, required=True, help="Resolve project name")

    relink_parser = subparsers.add_parser("relink-timeline-clips", help="Re-create timeline clips in Resolve")
    relink_parser.add_argument("--project", type=str, required=True, help="Resolve project name")
    relink_parser.add_argument("--notes-path", type=str, help="Path to editor notes JSON")
    relink_parser.add_argument("--report-path", type=str, help="Destination for relink report JSON")

    args = parser.parse_args()

    try:
        automation = ResolveAutomation(project_name=args.project)
        if args.command == "import-fcpxml":
            fcpxml_path = Path(args.fcpxml).resolve()
            print(f"Importing {fcpxml_path} into project '{args.project}' as '{args.timeline}'...")
            name = automation.import_fcpxml(
                fcpxml_path,
                timeline_name=args.timeline,
            )
            print(f"Successfully imported timeline: {name}")
        elif args.command == "relink-timeline-clips":
            notes_path = Path(args.notes_path).resolve() if args.notes_path else None
            report_path = Path(args.report_path).resolve() if args.report_path else None
            print("Running timeline relinker...")
            summary = automation.relink_timeline_clips(notes_path=notes_path, report_path=report_path)
            print(json.dumps(summary, indent=2))
    except ResolveNotAvailableError as exc:
        print(f"RESOLVE_UNAVAILABLE: {exc}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
