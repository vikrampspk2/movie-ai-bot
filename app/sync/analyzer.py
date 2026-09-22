from __future__ import annotations

import json
import subprocess
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
    reference_sample_rate: int
    candidate_sample_rate: int
    reference_channels: int
    candidate_channels: int
    reference_layout: str | None
    candidate_layout: str | None
    video_fps_reference: float | None
    video_fps_candidate: float | None
    vfr_reference: bool
    vfr_candidate: bool


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


def _audio_shape(path: Path) -> tuple[int, str | None, int]:
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=channels,channel_layout,sample_rate", "-of", "json", str(path)
    ])
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError(f"No audio stream found: {path.name}")
    stream = streams[0]
    channels = int(stream.get("channels") or 0)
    if channels < 1:
        raise ValueError(f"Invalid audio channel count: {path.name}")
    return channels, stream.get("channel_layout"), int(stream.get("sample_rate") or 0)


def _pcm(path: Path, sample_rate: int, channels: int, seconds: float = 120.0) -> np.ndarray:
    # No -ac downmix is used. FFmpeg outputs the native channel count in interleaved
    # PCM; the analyzer reshapes it into [samples, channels] for per-channel matching.
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path), "-t", str(seconds),
        "-ar", str(sample_rate), "-f", "f32le", "pipe:1",
    ]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True, timeout=300).stdout
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for PCM extraction") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.decode(errors="replace").strip() or "PCM extraction failed") from exc

    values = np.frombuffer(raw, dtype=np.float32)
    frames = values.size // channels
    if frames < sample_rate * 2:
        raise ValueError("Not enough audio for reliable synchronization")
    return values[:frames * channels].reshape(frames, channels)


def _normalize(x: np.ndarray) -> np.ndarray:
    x = x - np.mean(x)
    scale = np.sqrt(np.mean(x * x))
    return x / scale if scale > 1e-8 else x


def _channel_offset(reference: np.ndarray, candidate: np.ndarray, sample_rate: int) -> tuple[float, float]:
    n = min(reference.size, candidate.size, sample_rate * 120)
    a = _normalize(reference[:n])
    b = _normalize(candidate[:n])
    size = 1 << int((2 * n - 1).bit_length())
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)[:2 * n - 1]
    corr = np.concatenate((corr[-(n - 1):], corr[:n]))
    abs_corr = np.abs(corr)
    peak = int(np.argmax(abs_corr))
    lag = peak - (n - 1)
    peak_value = float(abs_corr[peak])
    radius = max(1, sample_rate // 4)
    masked = abs_corr.copy()
    masked[max(0, peak - radius):min(abs_corr.size, peak + radius + 1)] = 0
    runner = float(np.max(masked))
    confidence = min(1.0, max(0.0, peak_value / (runner + 1e-9) - 1.0))
    return lag / sample_rate, confidence


def estimate_offset(reference: np.ndarray, candidate: np.ndarray, sample_rate: int) -> tuple[float, float]:
    if reference.ndim != 2 or candidate.ndim != 2:
        raise ValueError("Audio arrays must be [samples, channels]")
    if reference.shape[1] != candidate.shape[1]:
        raise ValueError("Reference and candidate channel counts differ; refusing to downmix")
    offsets = []
    confidences = []
    for channel in range(reference.shape[1]):
        offset, confidence = _channel_offset(reference[:, channel], candidate[:, channel], sample_rate)
        offsets.append(offset)
        confidences.append(confidence)
    median = float(np.median(offsets))
    agreement = max(0.0, 1.0 - float(np.std(offsets)) / max(0.05, abs(median) + 0.05))
    confidence = min(1.0, float(np.median(confidences)) * agreement)
    return median, confidence


def analyze(reference: Path, candidate: Path, sample_rate: int = 8000) -> SyncAnalysis:
    ref_duration = _duration(reference)
    cand_duration = _duration(candidate)
    ref_channels, ref_layout, ref_rate = _audio_shape(reference)
    cand_channels, cand_layout, cand_rate = _audio_shape(candidate)
    if ref_channels != cand_channels:
        raise ValueError(f"Audio channel mismatch: reference={ref_channels}, candidate={cand_channels}")
    ref_pcm = _pcm(reference, sample_rate, ref_channels)
    cand_pcm = _pcm(candidate, sample_rate, cand_channels)
    offset, confidence = estimate_offset(ref_pcm, cand_pcm, sample_rate)
    drift = cand_duration - ref_duration
    layout_note = ref_layout or f"{ref_channels}ch"
    return SyncAnalysis(
        reference_duration=ref_duration,
        candidate_duration=cand_duration,
        estimated_offset_seconds=offset,
        confidence=confidence,
        drift_seconds=drift,
        sample_rate=sample_rate,
        channels=ref_channels,
        method=f"native-channel PCM FFT cross-correlation ({layout_note}) + duration drift check + source-rate/layout inspection",
        reference_sample_rate=ref_rate,
        candidate_sample_rate=cand_rate,
        reference_channels=ref_channels,
        candidate_channels=cand_channels,
        reference_layout=ref_layout,
        candidate_layout=cand_layout,
        video_fps_reference=None,
        video_fps_candidate=None,
        vfr_reference=False,
        vfr_candidate=False,
    )
