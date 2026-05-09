"""Thin wrappers around the DaVinci Resolve scripting API.

These utilities intentionally keep Resolve-specific calls in one module so the
rest of the codebase remains unit-testable without Resolve installed.
"""

from __future__ import annotations

import os
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from gateway.resolve.timeline_snapshot import TimelineSnapshot
from gateway.resolve.timeline_relinker import TimelineRelinker

# --- Auto-configuration for Resolve Environment ---
# This block ensures that whenever this module is imported, we attempt to:
# 1. Locate the repository root and add it to sys.path (critical for fuscript).
# 2. Load .env variables so connection parameters are available.
# 3. Check if we are running in standard Python. If so, AUTO-RELAUNCH via fuscript.exe.

try:
    # bridge.py is in src/gateway/resolve/
    # Repo root is 3 levels up from src/ (src/gateway/resolve/ -> src/gateway/ -> src/ -> root)
    _FILE_PATH = Path(__file__).resolve()
    REPO_ROOT = _FILE_PATH.parents[3]
    
    # Add repo root to sys.path if missing
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # Try loading .env
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    # --- AUTO-RELAUNCH LOGIC ---
    # If we are NOT running inside fuscript (checked via executable name),
    # we attempt to find fuscript.exe and relaunch the current script.
    if "fuscript" not in sys.executable.lower() and "pytest" not in sys.modules:
        resolve_script_lib = os.environ.get("RESOLVE_SCRIPT_LIB")
        resolve_script_api = os.environ.get("RESOLVE_SCRIPT_API")
        
        if resolve_script_lib:
            # Handle potential quotes in env var
            clean_lib = resolve_script_lib.strip('"').strip("'")
            fuscript_path = Path(clean_lib).parent / "fuscript.exe"
            
            if fuscript_path.exists():
                # Prepare environment for the child process
                env = os.environ.copy()
                current_pythonpath = env.get("PYTHONPATH", "")

                # Clean up API path string
                api_path_str = resolve_script_api.strip('"').strip("'") if resolve_script_api else ""
                modules_path = str(Path(api_path_str) / "Modules") if api_path_str else ""

                # Ensure PYTHONPATH has src/, repo root, and Resolve Modules
                pythonpath_parts = [
                    str(REPO_ROOT / "src"),
                    str(REPO_ROOT),
                ]
                if modules_path:
                    pythonpath_parts.append(modules_path)
                if current_pythonpath:
                    pythonpath_parts.append(current_pythonpath)

                env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
                
                # Relaunch
                print(f"Relaunching via fuscript: {fuscript_path}")
                # sys.argv[0] is the script that imported this module
                cmd = [str(fuscript_path)] + sys.argv
                
                try:
                    proc = subprocess.run(cmd, env=env)
                    sys.exit(proc.returncode)
                except Exception as e:
                    print(f"Failed to launch fuscript: {e}")
                    sys.exit(1)

except NameError:
    # Fallback for environments where __file__ is not defined (some embedded contexts)
    REPO_ROOT = Path(os.getcwd()).resolve()
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

RESOLVE_APP_NAME = "Resolve"


class ResolveNotAvailableError(RuntimeError):
    """Raised when the Resolve scripting API cannot be loaded."""


def _bootstrap_resolve_environment() -> None:
    """Ensure Resolve's scripting modules and DLLs are discoverable."""

    script_api_root = os.environ.get("RESOLVE_SCRIPT_API")
    if script_api_root:
        normalized_api_root = os.path.expandvars(script_api_root.strip("\""))
        os.environ["RESOLVE_SCRIPT_API"] = normalized_api_root
        modules_dir = Path(normalized_api_root) / "Modules"
        if modules_dir.exists() and str(modules_dir) not in sys.path:
            sys.path.append(str(modules_dir))

    script_lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if script_lib and hasattr(os, "add_dll_directory"):
        normalized_script_lib = os.path.expandvars(script_lib.strip("\""))
        os.environ["RESOLVE_SCRIPT_LIB"] = normalized_script_lib
        dll_dir = Path(normalized_script_lib).parent
        if dll_dir.exists():
            os.add_dll_directory(str(dll_dir))


@dataclass
class ResolveBridge:
    """Encapsulates Resolve scripting operations used in the MVP loop."""

    project_name: str
    _resolve: Any | None = field(default=None, init=False, repr=False)
    _project: Any | None = field(default=None, init=False, repr=False)

    def connect(self) -> None:
        """Connect to Resolve via ``DaVinciResolveScript``."""

        if self._resolve:
            return
        _bootstrap_resolve_environment()
        try:
            import DaVinciResolveScript  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover - requires Resolve
            raise ResolveNotAvailableError(
                "DaVinciResolveScript is not on PYTHONPATH. See section 0 of "
                "editing system onboarding.md for setup steps."
            ) from exc
        except SystemExit as exc:  # pragma: no cover - Resolve helper exits when misconfigured
            raise ResolveNotAvailableError(
                "Resolve's scripting bootstrapper exited early. Double-check that "
                "you are running inside Resolve's Fusion Scripts environment."
            ) from exc
        try:
            self._resolve = DaVinciResolveScript.scriptapp(RESOLVE_APP_NAME)
        except SystemExit as exc:  # pragma: no cover - Resolve scripting edge cases
            raise ResolveNotAvailableError(
                "Resolve scripting exited unexpectedly. Ensure Resolve is running "
                "and external scripting is enabled."
            ) from exc
        if self._resolve is None:
            raise ResolveNotAvailableError(
                "Unable to acquire Resolve scripting handle. Ensure Resolve is "
                "running and scripting is enabled."
            )

    def _get_project_manager(self):
        self.connect()
        project_manager = self._resolve.GetProjectManager()
        if project_manager is None:
            raise RuntimeError("Resolve project manager is unavailable.")
        return project_manager

    def _ensure_project_loaded(self):
        if self._project is not None:
            return self._project

        project_manager = self._get_project_manager()
        current_project = project_manager.GetCurrentProject()
        if current_project and current_project.GetName() == self.project_name:
            self._project = current_project
            return current_project

        project = None
        if self.project_name:
            loaded = project_manager.LoadProject(self.project_name)
            if loaded:
                project = loaded if hasattr(loaded, "GetName") else project_manager.GetCurrentProject()
        if project is None and self.project_name:
            created = project_manager.CreateProject(self.project_name)
            if created:
                project = created if hasattr(created, "GetName") else project_manager.GetCurrentProject()

        if project is None:
            raise RuntimeError(
                f"Unable to load or create Resolve project '{self.project_name}'."
            )

        self._project = project
        return project

    # --- Phase 1 helpers -------------------------------------------------
    def import_fcpxml(self, fcpxml_path: Path, timeline_name: Optional[str] = None) -> str:
        """Import an FCPXML into the active project."""

        project = self._ensure_project_loaded()
        media_pool = project.GetMediaPool()
        if media_pool is None:
            raise RuntimeError("Unable to access Resolve media pool.")
        # If a specific name is requested, delete any existing timeline with that name
        # to ensure we replace it rather than creating a duplicate/suffixed version.
        if timeline_name:
            timelines_to_delete = []
            for idx in range(1, project.GetTimelineCount() + 1):
                timeline = project.GetTimelineByIndex(idx)
                if timeline and timeline.GetName() == timeline_name:
                    timelines_to_delete.append(timeline)
            
            if timelines_to_delete:
                media_pool.DeleteTimelines(timelines_to_delete)

        import_options: dict[str, object] = {}
        if timeline_name:
            import_options["timelineName"] = timeline_name

        # Ensure Resolve pulls media referenced in the FCPXML if it's missing.
        import_options.setdefault("importSourceClips", True)
        timeline = media_pool.ImportTimelineFromFile(str(fcpxml_path), import_options)
        if timeline is None:
            # Refresh existing names for the error message
            existing_names = []
            for idx in range(1, project.GetTimelineCount() + 1):
                t = project.GetTimelineByIndex(idx)
                if t: existing_names.append(t.GetName())

            raise RuntimeError(
                "Resolve media pool failed to import timeline from "
                f"{fcpxml_path}. Existing timelines: {sorted(existing_names)}"
            )
        project.SetCurrentTimeline(timeline)
        final_name = timeline_name or timeline.GetName()
        if final_name and timeline.GetName() != final_name:
            timeline.SetName(final_name)
        return timeline.GetName()

    def render_timeline(
        self,
        output_path: Path,
        preset: str = "YouTube 1080p",
        format_: str = "mp4",
        codec: str = "H.264",
    ) -> Path:
        """Trigger a render of the current timeline using Resolve's Deliver page."""

        project = self._ensure_project_loaded()
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            raise RuntimeError("No active timeline to render.")
        project.SetCurrentRenderFormatAndCodec(format_, codec)
        project.SetCurrentRenderMode(1)
        project.LoadRenderPreset(preset)
        project.SetRenderSettings(
            {
                "TargetDir": str(output_path.parent),
                "CustomName": output_path.stem,
            }
        )
        project.DeleteAllRenderJobs()
        job_id = project.AddRenderJob()
        if job_id is None:
            raise RuntimeError("Resolve failed to enqueue render job.")
        project.StartRendering(job_id)
        while project.IsRenderingInProgress():  # pragma: no cover - needs Resolve
            pass
        project.DeleteRenderJob(job_id)
        return _find_rendered_output(output_path)

    def export_timeline_archive(self, output_path: Path) -> Path:
        """Export the active timeline to Resolve's native format (DRT)."""

        project = self._ensure_project_loaded()
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            raise RuntimeError("No active timeline to export.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        export_type = getattr(self._resolve, "EXPORT_DRT", None)
        export_subtype = getattr(self._resolve, "EXPORT_NONE", 0)
        if export_type is None:
            raise RuntimeError("Resolve scripting constants missing EXPORT_DRT.")
        success = timeline.Export(str(output_path), export_type, export_subtype)
        if not success:
            raise RuntimeError(f"Failed to export timeline to {output_path}")
        return output_path

    # --- Timeline snapshot helpers ---------------------------------------
    def capture_timeline_snapshot(self, destination: Path | None = None) -> dict[str, Any]:
        """Capture the current timeline into a serializable snapshot."""

        project = self._ensure_project_loaded()
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            raise RuntimeError("No active timeline to snapshot.")
        snapshot = TimelineSnapshot.capture(project, timeline)
        if destination:
            destination = destination.resolve()
            snapshot.to_json(destination)
            return {"snapshot_path": str(destination), "timeline_name": snapshot.name}
        return snapshot.to_dict()

    def rebuild_timeline_from_snapshot(
        self,
        snapshot_source: Path | dict[str, Any] | TimelineSnapshot,
        *,
        timeline_name: str | None = None,
        wipe_media_pool: bool = True,
        delete_timelines: bool = True,
    ) -> str:
        """Recreate a timeline from a previously captured snapshot."""

        project = self._ensure_project_loaded()
        snapshot = self._coerce_snapshot(snapshot_source)
        if timeline_name:
            snapshot.name = timeline_name
        if wipe_media_pool:
            self.wipe_media_pool(delete_timelines=delete_timelines)
        timeline = snapshot.rebuild(project, self._resolve)
        return timeline.GetName()

    def wipe_media_pool(self, *, delete_timelines: bool = True) -> None:
        """Remove all clips/folders (and optionally timelines) from the media pool."""

        project = self._ensure_project_loaded()
        media_pool = project.GetMediaPool()
        if media_pool is None:
            raise RuntimeError("Unable to access Resolve media pool.")
        root_folder = media_pool.GetRootFolder()
        if root_folder is None:
            raise RuntimeError("Resolve media pool root folder unavailable.")
        self._purge_folder(media_pool, root_folder, delete_self=False)
        if delete_timelines:
            timelines_to_delete = [
                project.GetTimelineByIndex(idx)
                for idx in range(1, (project.GetTimelineCount() or 0) + 1)
            ]
            timelines = [t for t in timelines_to_delete if t is not None]
            if timelines:
                media_pool.DeleteTimelines(timelines)

    # --- Internal helpers -------------------------------------------------
    def _coerce_snapshot(self, snapshot_source: Path | dict[str, Any] | TimelineSnapshot) -> TimelineSnapshot:
        if isinstance(snapshot_source, TimelineSnapshot):
            return snapshot_source
        if isinstance(snapshot_source, (str, Path)):
            return TimelineSnapshot.from_json(Path(snapshot_source))
        if isinstance(snapshot_source, dict):
            return TimelineSnapshot.from_dict(snapshot_source)
        raise TypeError(f"Unsupported snapshot input: {type(snapshot_source)}")

    def _purge_folder(self, media_pool: Any, folder: Any, *, delete_self: bool) -> None:
        """Recursively delete the folder's clips and subfolders."""

        subfolders = folder.GetSubFolders() or {}
        if isinstance(subfolders, dict):
            subfolder_objs = list(subfolders.values())
        elif isinstance(subfolders, (list, tuple)):
            subfolder_objs = list(subfolders)
        else:
            subfolder_objs = []
        for subfolder in subfolder_objs:
            self._purge_folder(media_pool, subfolder, delete_self=True)

        clip_list = self._extract_clips(folder)
        if clip_list:
            media_pool.DeleteClips(clip_list)

        if delete_self:
            media_pool.DeleteFolders([folder])

    def relink_timeline_clips(
        self,
        *,
        notes_path: Path | None = None,
        report_path: Path | None = None,
    ) -> dict[str, Any]:
        project = self._ensure_project_loaded()
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            raise RuntimeError("No active timeline to relink.")
        relinker = TimelineRelinker(project, timeline)
        result = relinker.relink(notes_path=notes_path, report_path=report_path)
        return result.summary()

    @staticmethod
    def _extract_clips(folder: Any) -> list[Any]:
        clips = folder.GetClips() if hasattr(folder, "GetClips") else None
        if not clips and hasattr(folder, "GetClipList"):
            clips = folder.GetClipList()
        if clips is None:
            return []
        if isinstance(clips, dict):
            return [clip for clip in clips.values() if clip is not None]
        if isinstance(clips, (list, tuple)):
            return [clip for clip in clips if clip is not None]
        return []


def _find_rendered_output(requested_path: Path) -> Path:
    if requested_path.exists():
        return requested_path
    parent = requested_path.parent
    if not parent.exists():
        return requested_path
    matches = [path for path in parent.glob(f"{requested_path.stem}.*") if path.is_file()]
    if not matches:
        return requested_path
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]
