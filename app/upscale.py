from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class UpscaleError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise UpscaleError("FFmpeg is required for AI upscaling") from exc
    except subprocess.CalledProcessError as exc:
        raise UpscaleError(exc.stderr.strip() or "FFmpeg failed") from exc


def _probe(path: Path) -> dict:
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,pix_fmt",
        "-of", "json", str(path),
    ], timeout=120)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise UpscaleError("No video stream found")
    return streams[0]


def _gpu_available() -> bool:
    return shutil.which("nvidia-smi") is not None


def upscale_4k(source: Path, output: Path) -> dict:
    """Real-ESRGAN entry point with a safe media-preservation pipeline.

    The model invocation is deliberately isolated: if the model/runtime is not
    installed or weights are unavailable, this function fails instead of silently
    falling back to a normal resize and falsely calling it AI.
    """
    info = _probe(source)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if width >= 3840 and height >= 2160:
        raise UpscaleError("Source is already 4K or higher; AI upscale is not required")

    # Real-ESRGAN model/runtime integration is kept explicit. A deployment must
    # provide the model weights before enabling production execution.
    try:
        import torch  # noqa: F401
        from realesrgan import RealESRGAN  # type: ignore
    except ImportError as exc:
        raise UpscaleError("Real-ESRGAN runtime is not installed correctly") from exc

    if not _gpu_available() and not torch.cuda.is_available():
        raise UpscaleError("No supported GPU detected for the AI upscaling stage")

    raise UpscaleError(
        "Real-ESRGAN model weights/runtime adapter must be configured before production execution; "
        "normal resize fallback is intentionally disabled"
    )
