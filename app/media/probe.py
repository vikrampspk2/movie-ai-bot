from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class VideoInfo:
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration: float | None = None
    bitrate: int | None = None
    pix_fmt: str | None = None
    hdr: str | None = None


@dataclass(slots=True)
class AudioInfo:
    codec: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    bitrate: int | None = None
    duration: float | None = None


@dataclass(slots=True)
class MediaInfo:
    container: str | None
    format_name: str | None
    duration: float | None
    size_bytes: int | None
    video: VideoInfo | None
    audio: list[AudioInfo]

    def to_dict(self) -> dict:
        return asdict(self)


def _ratio(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        a, b = value.split("/", 1)
        return float(a) / float(b) if float(b) else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path: Path) -> MediaInfo:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is required but was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffprobe failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe timed out") from exc

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    v = None
    if videos:
        s = videos[0]
        tags = s.get("tags", {})
        hdr = tags.get("DOVI_PROFILE") or tags.get("HDR_FORMAT") or s.get("color_transfer")
        v = VideoInfo(
            codec=s.get("codec_name"), width=s.get("width"), height=s.get("height"),
            fps=_ratio(s.get("avg_frame_rate") or s.get("r_frame_rate")),
            duration=float(s["duration"]) if s.get("duration") else None,
            bitrate=int(s["bit_rate"]) if s.get("bit_rate", "").isdigit() else None,
            pix_fmt=s.get("pix_fmt"), hdr=hdr,
        )

    audio = []
    for s in audios:
        audio.append(AudioInfo(
            codec=s.get("codec_name"), channels=s.get("channels"),
            channel_layout=s.get("channel_layout"),
            sample_rate=int(s["sample_rate"]) if s.get("sample_rate", "").isdigit() else None,
            bitrate=int(s["bit_rate"]) if s.get("bit_rate", "").isdigit() else None,
            duration=float(s["duration"]) if s.get("duration") else None,
        ))

    return MediaInfo(
        container=fmt.get("format_long_name"), format_name=fmt.get("format_name"),
        duration=float(fmt["duration"]) if fmt.get("duration") else None,
        size_bytes=int(fmt["size"]) if fmt.get("size", "").isdigit() else None,
        video=v, audio=audio,
    )
