from __future__ import annotations

import subprocess
from pathlib import Path

from .analyzer import SyncAnalysis, analyze


class SyncVerificationError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3600)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for synchronization") from exc
    except subprocess.CalledProcessError as exc:
        raise SyncVerificationError(exc.stderr.strip() or "FFmpeg synchronization failed") from exc


def _probe_audio_layout(video: Path) -> list[dict]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index,codec_name,channels,channel_layout,sample_rate",
        "-of", "json", str(video),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SyncVerificationError("Could not inspect source audio layout") from exc
    import json
    return json.loads(result.stdout).get("streams", [])


def apply_sync(video: Path, analysis: SyncAnalysis, output: Path) -> None:
    """Apply correction without downmixing.

    Filtered audio must be encoded; stream-copy cannot be combined with an audio
    filter. The source channel count/layout is explicitly preserved by FFmpeg's
    channel layout negotiation and verified after rendering.
    """
    offset = analysis.estimated_offset_seconds
    streams = _probe_audio_layout(video)
    if not streams:
        raise SyncVerificationError("No audio stream found")

    if abs(offset) < 0.005:
        _run([
            "ffmpeg", "-v", "error", "-i", str(video),
            "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
            "-y", str(output),
        ])
        return

    # Use per-stream filters. adelay is channel-preserving when all=1.
    if offset > 0:
        filter_expr = f"[0:a:0]asetpts=PTS-{offset}/TB[a0]"
    else:
        delay_ms = max(0, int(round(-offset * 1000)))
        filter_expr = f"[0:a:0]adelay={delay_ms}:all=1[a0]"

    # PCM intermediate is lossless and keeps the original channel count/layout.
    # The final encode uses FLAC so sync correction does not introduce lossy audio.
    _run([
        "ffmpeg", "-v", "error", "-i", str(video),
        "-filter_complex", filter_expr,
        "-map", "0:v?", "-map", "[a0]", "-map", "0:s?",
        "-c:v", "copy", "-c:a", "flac", "-c:s", "copy",
        "-map_metadata", "0", "-y", str(output),
    ])

    after = _probe_audio_layout(output)
    before_layout = [(s.get("channels"), s.get("channel_layout")) for s in streams]
    after_layout = [(s.get("channels"), s.get("channel_layout")) for s in after]
    if before_layout != after_layout:
        raise SyncVerificationError(
            f"Audio layout changed during sync: before={before_layout}, after={after_layout}"
        )


def sync_and_verify(reference: Path, candidate: Path, output: Path) -> SyncAnalysis:
    analysis = analyze(reference, candidate)
    if analysis.confidence < 0.10:
        raise SyncVerificationError("Synchronization confidence is too low for automatic correction")
    if abs(analysis.drift_seconds) > 0.5:
        raise SyncVerificationError("Duration drift is too large for constant-offset correction")

    apply_sync(candidate, analysis, output)
    verified = analyze(reference, output)
    tolerance = max(0.08, abs(analysis.estimated_offset_seconds) * 0.35)
    if abs(verified.estimated_offset_seconds) > tolerance:
        raise SyncVerificationError("Post-sync verification did not confirm the correction")
    return verified
