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


def apply_sync(video: Path, analysis: SyncAnalysis, output: Path) -> None:
    """Apply a conservative constant correction while preserving audio channels.

    The source audio is never downmixed. Stream-copy is not used when a timestamp
    correction requires re-mux/re-encode; verification must follow this operation.
    """
    offset = analysis.estimated_offset_seconds
    if abs(offset) < 0.005:
        # No meaningful correction: remux without touching audio samples.
        _run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", "-y", str(output)])
        return

    if offset > 0:
        # Candidate audio starts later; advance it by offset.
        audio_filter = f"asetpts=PTS-{offset}/TB"
    else:
        # Candidate audio starts earlier; delay it. apad prevents truncation.
        delay_ms = int(round(-offset * 1000))
        audio_filter = f"adelay={delay_ms}:all=1"

    _run([
        "ffmpeg", "-v", "error", "-i", str(video), "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
        "-c:v", "copy", "-af", audio_filter, "-c:a", "copy", "-c:s", "copy", "-y", str(output),
    ])


def sync_and_verify(reference: Path, candidate: Path, output: Path) -> SyncAnalysis:
    analysis = analyze(reference, candidate)
    if analysis.confidence < 0.10:
        raise SyncVerificationError("Synchronization confidence is too low for an automatic correction")
    if abs(analysis.drift_seconds) > 0.5:
        raise SyncVerificationError("Duration drift is too large for a constant-offset correction")
    apply_sync(candidate, analysis, output)
    verified = analyze(reference, output)
    if abs(verified.estimated_offset_seconds) > max(0.08, abs(analysis.estimated_offset_seconds) * 0.35):
        raise SyncVerificationError("Post-sync verification did not confirm the correction")
    return verified
