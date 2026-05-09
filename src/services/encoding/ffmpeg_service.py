"""FFmpeg-based encoding helpers to keep media workloads centralized."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, NamedTuple
import re

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


class EncodingServiceError(RuntimeError):
    """Raised when the encoding service cannot fulfill a request."""


class _EncoderProfile(NamedTuple):
    name: str
    pre_input_flags: tuple[str, ...]
    vf_prefix: str | None
    codec_args: tuple[str, ...]


class FfmpegEncodingService:
    """Convenience wrapper around ffmpeg for slowdown/encoding tasks."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        enable_hwaccel: bool = True,
        default_suffix: str = ".mp4",
    ) -> None:
        self._explicit_exec = executable
        self.enable_hwaccel = enable_hwaccel
        self.default_suffix = default_suffix
        self._cached_encoders: set[str] | None = None

    def slowdown_video(
        self,
        source: Path,
        factor: float,
        *,
        output_path: Path | None = None,
        keep_audio: bool = False,
    ) -> Path:
        """Stretch the clip by `factor` using ffmpeg setpts."""
        if factor < 1.0:
            raise ValueError("Slowdown factor must be >= 1.0.")
        source = source.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)

        target = output_path
        if target is None:
            temp_file = tempfile.NamedTemporaryFile(suffix=self.default_suffix, delete=False)
            temp_file.close()
            target = Path(temp_file.name)
        else:
            target = output_path.expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)

        profile = self._select_profile()
        vf_filter = f"setpts={factor}*PTS"
        if profile.vf_prefix:
            vf_filter = f"{profile.vf_prefix},{vf_filter}"

        ffmpeg_bin = self._resolve_executable()
        cmd: list[str] = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *profile.pre_input_flags,
            "-i",
            str(source),
            "-vf",
            vf_filter,
            *profile.codec_args,
        ]
        if not keep_audio:
            cmd.append("-an")
        cmd += ["-progress", "pipe:1", str(target)]

        print(f"[encoding] Running ffmpeg slowdown via {profile.name} ...")
        self._run_with_progress(cmd)
        return target

    def extract_frame(
        self,
        source: Path,
        timestamp: float,
        *,
        output_path: Path | None = None,
    ) -> Path:
        """Grab a single frame at `timestamp` seconds and return the image path."""
        source = source.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        ts = max(timestamp, 0.0)
        target = output_path
        if target is None:
            temp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            temp_file.close()
            target = Path(temp_file.name)
        else:
            target = output_path.expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg_bin = self._resolve_executable()
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{ts:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(target),
        ]
        self._run_simple(cmd)
        return target

    def extract_segment(
        self,
        source: Path,
        duration: float,
        *,
        output_path: Path | None = None,
    ) -> Path:
        """Create a short clip of `duration` seconds from the start of `source`."""
        if duration <= 0:
            raise ValueError("Segment duration must be positive.")
        source = source.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)

        target = output_path
        if target is None:
            temp_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            temp_file.close()
            target = Path(temp_file.name)
        else:
            target = output_path.expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg_bin = self._resolve_executable()
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            f"{duration:.2f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "32",
            "-an",
            str(target),
        ]
        self._run_simple(cmd)
        return target

    def cut_segment(
        self,
        source: Path,
        start: float,
        duration: float,
        *,
        output_path: Path | None = None,
    ) -> Path:
        """Cut a clip starting at `start` seconds with the provided `duration`."""

        if duration <= 0:
            raise ValueError("Segment duration must be positive.")
        start = max(start, 0.0)
        source = source.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)

        target = output_path
        if target is None:
            temp_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            temp_file.close()
            target = Path(temp_file.name)
        else:
            target = output_path.expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg_bin = self._resolve_executable()
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "32",
            "-an",
            str(target),
        ]
        self._run_simple(cmd)
        return target

    def _resolve_executable(self) -> str:
        if self._explicit_exec:
            return self._explicit_exec
        env_override = os.getenv("FFMPEG_PATH")
        if env_override:
            return env_override

        local_candidate = self._find_repo_ffmpeg()
        if local_candidate:
            return str(local_candidate)

        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise EncodingServiceError(
                "ffmpeg executable not found. Install ffmpeg or set FFMPEG_PATH to its location."
            )
        return ffmpeg_path

    def _find_repo_ffmpeg(self) -> Path | None:
        try:
            repo_root = Path(__file__).resolve().parents[3]
        except IndexError:
            return None
        tools_dir = repo_root / "tools" / "ffmpeg"
        if not tools_dir.exists():
            return None
        exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

        direct_candidate = tools_dir / "bin" / exe_name
        if direct_candidate.exists():
            return direct_candidate
        for child in tools_dir.iterdir():
            candidate = child / "bin" / exe_name
            if candidate.exists():
                return candidate
        return None

    def _detect_vaapi_device(self) -> Path | None:
        if os.name == "nt":
            return None
        device = Path("/dev/dri/renderD128")
        if device.exists():
            return device
        return None

    def _supports_windows_hw(self) -> bool:
        if os.getenv("FFMPEG_DISABLE_AMF") == "1":
            return False
        return sys.platform.startswith("win")

    def _select_profile(self) -> _EncoderProfile:
        if not self.enable_hwaccel:
            return self._cpu_profile()

        encoder_pref = os.getenv("FFMPEG_ENCODER", "auto").lower()
        vaapi_device = self._detect_vaapi_device()
        if encoder_pref == "cpu":
            return self._cpu_profile()
        if encoder_pref == "vaapi":
            if not vaapi_device:
                raise EncodingServiceError("FFMPEG_ENCODER=vaapi but no VAAPI device was detected.")
            return self._vaapi_profile(vaapi_device)
        if encoder_pref == "amf":
            if not self._supports_windows_hw():
                raise EncodingServiceError("FFMPEG_ENCODER=amf but Windows hardware acceleration is unavailable.")
            return self._windows_hw_profile()

        # auto detection
        if self._supports_windows_hw():
            try:
                return self._windows_hw_profile()
            except EncodingServiceError as exc:
                print(f"[encoding] AMF not available ({exc}). Falling back to other encoders.")
        if vaapi_device:
            return self._vaapi_profile(vaapi_device)
        return self._cpu_profile()

    def _cpu_profile(self) -> _EncoderProfile:
        return _EncoderProfile(
            name="CPU(libx264)",
            pre_input_flags=(),
            vf_prefix=None,
            codec_args=("-c:v", "libx264", "-preset", "veryfast", "-crf", "32"),
        )

    def _vaapi_profile(self, device: Path) -> _EncoderProfile:
        return _EncoderProfile(
            name=f"VAAPI({device})",
            pre_input_flags=("-hwaccel", "vaapi", "-vaapi_device", str(device)),
            vf_prefix="format=nv12,hwupload",
            codec_args=("-c:v", "hevc_vaapi", "-qp", "26"),
        )

    def _windows_hw_profile(self) -> _EncoderProfile:
        codec_env = os.getenv("FFMPEG_AMF_CODEC")
        default_candidates = ["hevc_amf", "h264_amf", "h264_mf", "hevc_mf"]
        preferred_order = [codec_env] if codec_env else []
        preferred_order += default_candidates
        candidates: list[str] = []
        for encoder_name in preferred_order:
            if encoder_name not in candidates:
                candidates.append(encoder_name)
        for codec in candidates:
            if not codec:
                continue
            if self._encoder_available(codec):
                codec_args = ["-c:v", codec]
                if codec.endswith("_amf"):
                    codec_args += ["-usage", "transcoding", "-quality", "quality"]
                return _EncoderProfile(
                    name=f"WinHW({codec})",
                    pre_input_flags=(),
                    vf_prefix=None,
                    codec_args=tuple(codec_args),
                )
        raise EncodingServiceError(
            "No Windows hardware encoder (hevc_amf/h264_amf/hevc_mf/h264_mf) found in this ffmpeg build."
        )

    def _list_ffmpeg_encoders(self) -> set[str]:
        if self._cached_encoders is not None:
            return self._cached_encoders
        ffmpeg_bin = self._resolve_executable()
        try:
            completed = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-encoders"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=True,
            )
        except Exception:
            self._cached_encoders = set()
            return self._cached_encoders

        encoders: set[str] = set()
        for line in completed.stdout.splitlines():
            stripped = ANSI_ESCAPE_RE.sub("", line).strip()
            if not stripped or stripped.startswith("Encoders:") or stripped.startswith("------"):
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                encoders.add(parts[1])
            elif parts:
                encoders.add(parts[0])
        self._cached_encoders = encoders
        return encoders

    def _encoder_available(self, encoder: str) -> bool:
        return encoder in self._list_ffmpeg_encoders()
        return _EncoderProfile(
            name="AMF(hevc_amf)",
            pre_input_flags=(),
            vf_prefix=None,
            codec_args=("-c:v", "hevc_amf", "-usage", "transcoding", "-quality", "quality"),
        )

    def _run_with_progress(self, cmd: Iterable[str]) -> None:
        try:
            process = subprocess.Popen(
                list(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise EncodingServiceError(f"Failed to start ffmpeg: {exc}") from exc

        try:
            if not process.stdout:
                raise EncodingServiceError("ffmpeg did not expose stdout for progress updates.")
            for line in process.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    value = line.split("=", 1)[1]
                    try:
                        current = float(value)
                    except ValueError:
                        continue
                    print(f"[encoding] Processed {current / 1000.0:.2f}s")
                elif line.startswith("progress=") and line.endswith("end"):
                    print("[encoding] Slowdown completed.")
            stderr_output = process.stderr.read() if process.stderr else ""
            return_code = process.wait()
            if return_code != 0:
                raise EncodingServiceError(f"ffmpeg exited with {return_code}: {stderr_output}")
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def _run_simple(self, cmd: Iterable[str]) -> None:
        try:
            completed = subprocess.run(
                list(cmd),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise EncodingServiceError(
                f"ffmpeg command failed with code {exc.returncode}: {exc.stderr}"
            ) from exc
