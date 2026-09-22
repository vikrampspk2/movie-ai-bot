from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(slots=True)
class SyncAnalysis:
    reference_duration: float
    candidate_duration: float
    estimated_offset_seconds: float
    confidence: float
    drift_seconds: float
    sample_rate: int
    channels: int
    method: str


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=300)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg/ffprobe is required for synchronization") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or "media command failed") from exc


def _duration(path: Path) -> float:
    result = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    value = json.loads(result.stdout)["format"].get("duration")
    if value is None:
        raise ValueError(f"Could not determine duration: {path.name}")
    return float(value)


def _pcm(path: Path, sample_rate: int = 8000, channels: int = 1, seconds: float = 120.0) -> np.ndarray:
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path), "-t", str(seconds),
        "-vn", "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "pipe:1",
    ]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True, timeout=300).stdout
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for PCM extraction") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.decode(errors="replace").strip() or "PCM extraction failed") from exc
    return np.frombuffer(raw, dtype=np.float32)


def _normalize(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    x = x - np.mean(x)
    scale = np.sqrt(np.mean(x * x))
    return x / scale if scale > 1e-8 else x


def estimate_offset(reference: np.ndarray, candidate: np.ndarray, sample_rate: int) -> tuple[float, float]:
    """Estimate a constant offset using normalized FFT cross-correlation.

    Positive offset means candidate audio is estimated to start later than reference.
    Confidence is a bounded peak-to-runner-up ratio, not a guarantee of correctness.
    """
    n = min(reference.size, candidate.size)
    if n < sample_rate * 2:
        raise ValueError("Not enough audio for reliable synchronization")
    # Use a bounded analysis window to keep CPU/memory predictable.
    n = min(n, sample_rate * 120)
    a = _normalize(reference[:n])
    b = _normalize(candidate[:n])
    size = 1 << int((2 * n - 1).bit_length())
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)[: 2 * n - 1]
    corr = np.concatenate((corr[-(n - 1):], corr[:n]))
    abs_corr = np.abs(corr)
    peak = int(np.argmax(abs_corr))
    lag = peak - (n - 1)
    peak_value = float(abs_corr[peak])
    radius = max(1, sample_rate // 4)
    lo, hi = max(0, peak - radius), min(abs_corr.size, peak + radius + 1)
    masked = abs_corr.copy()
    masked[lo:hi] = 0
    runner = float(np.max(masked))
    confidence = min(1.0, max(0.0, peak_value / (runner + 1e-9) - 1.0))
    return lag / sample_rate, confidence


def analyze(reference: Path, candidate: Path, sample_rate: int = 8000) -> SyncAnalysis:
    ref_duration = _duration(reference)
    cand_duration = _duration(candidate)
    ref_pcm = _pcm(reference, sample_rate=sample_rate)
    cand_pcm = _pcm(candidate, sample_rate=sample_rate)
    offset, confidence = estimate_offset(ref_pcm, cand_pcm, sample_rate)
    drift = cand_duration - ref_duration
    return SyncAnalysis(
        reference_duration=ref_duration,
        candidate_duration=cand_duration,
        estimated_offset_seconds=offset,
        confidence=confidence,
        drift_seconds=drift,
        sample_rate=sample_rate,
        channels=1,
        method="PCM normalized FFT cross-correlation + duration drift check",
    )
