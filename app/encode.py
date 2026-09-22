from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class EncodeError(RuntimeError):
    pass


@dataclass(slots=True)
class EncodeResult:
    output: Path
    codec: str
    video_bitrate_kbps: int
    target_bytes: int
    actual_bytes: int


def _run(cmd: list[str], timeout: int = 86400) -> None:
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise EncodeError("FFmpeg is required for encoding") from exc
    except subprocess.CalledProcessError as exc:
        raise EncodeError(exc.stderr.strip() or "FFmpeg encoding failed") from exc


def _duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=120,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception as exc:
        raise EncodeError("Could not determine source duration") from exc


def _has_nvenc() -> bool:
    return shutil.which("nvidia-smi") is not None


def _target_bitrate(duration: float, target_min_gb: float = 3.0, target_max_gb: float = 5.0,
                    audio_subtitle_overhead_kbps: int = 1024) -> int:
    if duration <= 0:
        raise EncodeError("Invalid duration")
    # Choose the middle of the requested 3–5 GB range, then reserve room for
    # audio/subtitles/container overhead. Final size is measured and reported;
    # exact size cannot be guaranteed because stream overhead varies.
    target_bytes = int(((target_min_gb + target_max_gb) / 2) * (1024 ** 3))
    total_kbps = int((target_bytes * 8) / duration / 1000)
    return max(500, total_kbps - audio_subtitle_overhead_kbps)


def encode_to_mkv(source: Path, output: Path, target_min_gb: float = 3.0, target_max_gb: float = 5.0) -> EncodeResult:
    duration = _duration(source)
    bitrate = _target_bitrate(duration, target_min_gb, target_max_gb)

    if _has_nvenc():
        codec = "hevc_nvenc"
        preset = "p5"
        args = ["-c:v", codec, "-preset", preset, "-b:v", f"{bitrate}k",
                "-maxrate", f"{int(bitrate * 1.15)}k", "-bufsize", f"{int(bitrate * 2)}k"]
    else:
        codec = "libx265"
        args = ["-c:v", codec, "-preset", "medium", "-b:v", f"{bitrate}k",
                "-maxrate", f"{int(bitrate * 1.15)}k", "-bufsize", f"{int(bitrate * 2)}k"]

    _run([
        "ffmpeg", "-v", "error", "-i", str(source),
        "-map", "0", "-c:a", "copy", "-c:s", "copy",
        *args, "-map_metadata", "0", "-y", str(output),
    ])

    actual = output.stat().st_size
    if actual < int(target_min_gb * 1024**3) or actual > int(target_max_gb * 1024**3 * 1.08):
        raise EncodeError(f"Output size {actual / 1024**3:.2f} GiB is outside requested 3–5 GiB target")
    return EncodeResult(output, codec, bitrate, int(((target_min_gb + target_max_gb) / 2) * 1024**3), actual)
