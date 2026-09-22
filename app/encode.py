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
    attempts: int


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


def _audio_bitrate(path: Path) -> int:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=bit_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True, timeout=120)
        return sum(int(x) for x in r.stdout.splitlines() if x.strip().isdigit()) // 1000
    except Exception:
        return 1024


def _video_bitrate(duration: float, target_bytes: int, audio_kbps: int) -> int:
    usable = target_bytes * 8 / duration / 1000
    return max(500, int(usable - audio_kbps - 192))


def _encode_once(source: Path, output: Path, codec: str, bitrate: int) -> None:
    if codec == "hevc_nvenc":
        video = ["-c:v", codec, "-preset", "p5", "-b:v", f"{bitrate}k",
                 "-maxrate", f"{int(bitrate * 1.08)}k", "-bufsize", f"{int(bitrate * 2)}k"]
    else:
        video = ["-c:v", codec, "-preset", "medium", "-b:v", f"{bitrate}k",
                 "-maxrate", f"{int(bitrate * 1.08)}k", "-bufsize", f"{int(bitrate * 2)}k"]
    _run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0",
          "-c:a", "copy", "-c:s", "copy", *video, "-map_metadata", "0",
          "-y", str(output)])


def encode_to_mkv(source: Path, output: Path, target_min_gb: float = 3.0,
                  target_max_gb: float = 5.0, max_attempts: int = 3) -> EncodeResult:
    duration = _duration(source)
    target_bytes = int(((target_min_gb + target_max_gb) / 2) * 1024**3)
    audio_kbps = _audio_bitrate(source)
    bitrate = _video_bitrate(duration, target_bytes, audio_kbps)
    codec = "hevc_nvenc" if _has_nvenc() else "libx265"

    best_size = 0
    best_attempt = 0
    for attempt in range(1, max_attempts + 1):
        temp = output.with_name(f".{output.stem}.attempt{attempt}.mkv")
        _encode_once(source, temp, codec, bitrate)
        actual = temp.stat().st_size
        best_size, best_attempt = actual, attempt
        if target_min_gb * 1024**3 <= actual <= target_max_gb * 1024**3:
            temp.replace(output)
            return EncodeResult(output, codec, bitrate, target_bytes, actual, attempt)
        ratio = target_bytes / max(1, actual)
        bitrate = max(500, int(bitrate * ratio * 0.97))
        temp.unlink(missing_ok=True)

    raise EncodeError(
        f"Could not reach requested 3–5 GiB range after {max_attempts} attempts; "
        f"last output was {best_size / 1024**3:.2f} GiB. No out-of-range file was published."
    )
