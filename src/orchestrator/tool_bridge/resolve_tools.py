"""Resolve automation helpers intended for orchestrator tool calls.

This module acts as a client that dispatches commands to the Resolve process
via ``fuscript.exe`` and the ``gateway.resolve.runner`` script.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


class ResolveTools:
    """Client facade for Resolve automation running in a separate process."""

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        # src/orchestrator/tool_bridge/resolve_tools.py -> src/orchestrator/tool_bridge -> src/orchestrator -> src
        # runner is at src/gateway/resolve/runner.py
        self._runner_path = Path(__file__).parents[2] / "gateway" / "resolve" / "runner.py"
        self._src_root = Path(__file__).parents[2]
        self._repo_root = Path(__file__).parents[3]
        self._fuscript_path = self._find_fuscript()

    def _find_fuscript(self) -> Path:
        # Try to derive from RESOLVE_SCRIPT_LIB
        lib_path = os.environ.get("RESOLVE_SCRIPT_LIB")
        if lib_path:
            # Handle quotes if present
            if (lib_path.startswith('"') and lib_path.endswith('"')) or (lib_path.startswith("'") and lib_path.endswith("'")):
                lib_path = lib_path[1:-1]
            fuscript = Path(lib_path).parent / "fuscript.exe"
            if fuscript.exists():
                return fuscript
        
        # Fallback to default location
        default = Path("C:/Program Files/Blackmagic Design/DaVinci Resolve/fuscript.exe")
        if default.exists():
            return default
            
        raise RuntimeError("Could not locate fuscript.exe. Check RESOLVE_SCRIPT_LIB in .env")

    def _run_command(self, method: str, **kwargs) -> Any:
        # Serialize kwargs
        json_kwargs = {}
        for k, v in kwargs.items():
            if isinstance(v, Path):
                json_kwargs[k] = str(v)
            else:
                json_kwargs[k] = v
                
        cmd = [
            str(self._fuscript_path),
            str(self._runner_path),
            "--project", self.project_name,
            "--method", method,
            "--kwargs", json.dumps(json_kwargs)
        ]
        
        # Run fuscript
        # We capture stdout for the JSON result. Stderr is for logging/errors.
        result = subprocess.run(cmd, capture_output=True, text=True, env=self._subprocess_env())
        
        # Parse stdout for JSON. fuscript prints a banner, so we need to find the JSON line.
        # We look for the last non-empty line that starts with '{'
        json_output = None
        for line in reversed(result.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                json_output = line
                break
        
        if not json_output:
             raise RuntimeError(f"No JSON output found from fuscript (code {result.returncode}):\nStdout: {result.stdout}\nStderr: {result.stderr}")

        try:
            out = json.loads(json_output)
        except json.JSONDecodeError:
             raise RuntimeError(f"Invalid JSON from fuscript:\n{json_output}\nStderr: {result.stderr}")
             
        if out.get("status") == "error":
             raise RuntimeError(f"Resolve Error: {out.get('message')}\n{out.get('traceback')}")
             
        return out.get("result")

    def import_fcpxml(self, fcpxml_path: Path, *, timeline_name: str | None = None) -> str:
        return self._run_command("import_fcpxml", fcpxml_path=fcpxml_path, timeline_name=timeline_name)

    def render(self, destination: Path, *, preset: str = "YouTube 1080p", format_: str = "mp4", codec: str = "H.264") -> Path:
        result = self._run_command("render", destination=destination, preset=preset, format_=format_, codec=codec)
        return Path(result)

    def export_timeline_archive(self, destination: Path) -> Path:
        result = self._run_command("export_timeline_archive", destination=destination)
        return Path(result)

    def capture_timeline_snapshot(self, destination: Path | None = None) -> dict[str, Any]:
        kwargs = {}
        if destination is not None:
            kwargs["destination"] = destination
        result = self._run_command("capture_timeline_snapshot", **kwargs)
        if "snapshot_path" in result:
            result["snapshot_path"] = Path(result["snapshot_path"])
        return result

    def rebuild_timeline_from_snapshot(
        self,
        snapshot_path: Path | dict[str, Any],
        *,
        timeline_name: str | None = None,
        wipe_media_pool: bool = True,
        delete_timelines: bool = True,
    ) -> str:
        kwargs: dict[str, Any] = {
            "snapshot_path": snapshot_path,
            "timeline_name": timeline_name,
            "wipe_media_pool": wipe_media_pool,
            "delete_timelines": delete_timelines,
        }
        # Remove optional None entries to keep JSON clean
        if timeline_name is None:
            kwargs.pop("timeline_name")
        result = self._run_command("rebuild_timeline_from_snapshot", **kwargs)
        return str(result)

    def wipe_media_pool(self, *, delete_timelines: bool = True) -> None:
        self._run_command("wipe_media_pool", delete_timelines=delete_timelines)

    def relink_timeline_clips(self, *, notes_path: Path | None = None, report_path: Path | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if notes_path is not None:
            kwargs["notes_path"] = notes_path
        if report_path is not None:
            kwargs["report_path"] = report_path
        return self._run_command("relink_timeline_clips", **kwargs)

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
        result = self._run_command(
            "import_render_export",
            fcpxml_path=fcpxml_path,
            render_path=render_path,
            archive_path=archive_path,
            timeline_name=timeline_name,
            preset=preset,
            format_=format_,
            codec=codec,
        )
        # Convert paths back to Path objects
        if "render_path" in result:
            result["render_path"] = Path(result["render_path"])
        if "archive_path" in result:
            result["archive_path"] = Path(result["archive_path"])
        return result

    def execute_request(self, method: str, **kwargs) -> Any:
        """Generic entry point for LLMs to call any method on the ResolveAutomation service.
        
        Args:
            method: The name of the method to call (e.g., 'get_project_info', 'import_fcpxml').
            **kwargs: Arguments to pass to the method.
        """
        return self._run_command(method, **kwargs)

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        required = [str(self._repo_root), str(self._src_root)]
        parts = [*required, *[part for part in existing.split(os.pathsep) if part]]
        deduped: list[str] = []
        seen: set[str] = set()
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(part)
        env["PYTHONPATH"] = os.pathsep.join(deduped)
        return env
