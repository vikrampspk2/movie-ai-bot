from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class UpscaleError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 86400) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise UpscaleError("FFmpeg/Real-ESRGAN runtime is required") from exc
    except subprocess.CalledProcessError as exc:
        raise UpscaleError(exc.stderr.strip() or "AI upscaling failed") from exc


def _probe(path: Path) -> dict:
    result = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], 120)
    return json.loads(result.stdout)


def _video(path: Path) -> dict:
    streams = [s for s in _probe(path).get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise UpscaleError("No video stream found")
    return streams[0]


def _audio_layouts(path: Path) -> list[tuple[int | None, str | None, str | None]]:
    return [(s.get("channels"), s.get("channel_layout"), s.get("sample_rate"))
            for s in _probe(path).get("streams", []) if s.get("codec_type") == "audio"]


def _has_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None


def _realesrgan_frame_dir(input_dir: Path, output_dir: Path, outscale: float, tile: int) -> None:
    # Use the upstream Real-ESRGAN inference script/API. No normal-resize fallback.
    script = shutil.which("inference_realesrgan.py")
    if not script:
        raise UpscaleError("Real-ESRGAN inference_realesrgan.py is not installed on PATH")
    output_dir.mkdir(parents=True, exist_ok=True)
    _run([
        "python", script, "-n", "RealESRGAN_x4plus",
        "-i", str(input_dir), "-o", str(output_dir),
        "-s", str(outscale), "-t", str(tile),
    ], 86400)


def upscale_4k(source: Path, output: Path, workspace: Path | None = None) -> dict:
    info = _video(source)
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    if width >= 3840 and height >= 2160:
        raise UpscaleError("Source is already 4K or higher")
    if not _has_gpu():
        raise UpscaleError("AI Upscale requires an NVIDIA GPU backend")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise UpscaleError("FFmpeg/FFprobe are required")

    root = workspace or source.parent / "upscale-work"
    frames_in, frames_out = root / "frames_in", root / "frames_out"
    frames_in.mkdir(parents=True, exist_ok=True)
    try:
        fps = info.get("avg_frame_rate") or info.get("r_frame_rate") or "24000/1001"
        _run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:0",
              "-vsync", "0", "-q:v", "2", str(frames_in / "frame_%08d.png")], 86400)
        scale = min(4.0, max(1.0, 3840 / max(1, width), 2160 / max(1, height)))
        if width >= 1920 and height >= 1080:
            scale = min(4.0, max(3840 / width, 2160 / height))
        tile = 256
        _realesrgan_frame_dir(frames_in, frames_out, scale, tile)
        if not list(frames_out.glob("*.png")):
            raise UpscaleError("Real-ESRGAN produced no frames")
        output.parent.mkdir(parents=True, exist_ok=True)
        _run(["ffmpeg", "-v", "error", "-framerate", str(fps), "-i",
              str(frames_out / "frame_%08d_out.png"), "-i", str(source),
              "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?", "-map", "1:d?",
              "-map_metadata", "1", "-c:v", "libx265", "-preset", "medium", "-crf", "18",
              "-c:a", "copy", "-c:s", "copy", "-c:d", "copy", "-shortest", "-y", str(output)], 86400)
        after = _video(output)
        out_w, out_h = int(after.get("width") or 0), int(after.get("height") or 0)
        if out_w < 3840 or out_h < 2160:
            raise UpscaleError(f"AI output is not 4K: {out_w}x{out_h}")
        if _audio_layouts(source) != _audio_layouts(output):
            raise UpscaleError("Audio channel/layout/sample-rate changed during AI upscale")
        return {"model": "RealESRGAN_x4plus", "width": out_w, "height": out_h, "audio_preserved": True}
    finally:
        shutil.rmtree(frames_in, ignore_errors=True)
        shutil.rmtree(frames_out, ignore_errors=True)
