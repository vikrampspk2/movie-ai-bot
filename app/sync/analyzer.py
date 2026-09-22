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
    segment_offsets: tuple[float, ...]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg/ffprobe is required for synchronization") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or "media command failed") from exc


def _probe(path: Path) -> dict:
    result = _run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ])
    return json.loads(result.stdout)


def _audio_shape(path: Path) -> tuple[int, str | None, int]:
    streams = [s for s in _probe(path).get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise ValueError(f"No audio stream found: {path.name}")
    s = streams[0]
    channels = int(s.get("channels") or 0)
    rate = int(s.get("sample_rate") or 0)
    if channels < 1 or rate < 1:
        raise ValueError(f"Invalid audio metadata: {path.name}")
    return channels, s.get("channel_layout"), rate


def _video_info(path: Path) -> tuple[float | None, bool]:
    streams = [s for s in _probe(path).get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        return None, False
    s = streams[0]
    rate = s.get("avg_frame_rate") or s.get("r_frame_rate")
    try:
        num, den = (int(x) for x in str(rate).split("/"))
        fps = num / den if den else None
    except Exception:
        fps = None
    avg = s.get("avg_frame_rate")
    real = s.get("r_frame_rate")
    vfr = bool(avg and real and avg != real)
    return fps, vfr


def _duration(path: Path) -> float:
    fmt = _probe(path).get("format", {})
    value = fmt.get("duration")
    if value is None:
        raise ValueError(f"Could not determine duration: {path.name}")
    return float(value)


def _pcm(path: Path, sample_rate: int, channels: int, start: float, seconds: float) -> np.ndarray:
    cmd = [
        "ffmpeg", "-v", "error", "-ss", str(max(0.0, start)), "-i", str(path),
        "-t", str(seconds), "-ar", str(sample_rate), "-f", "f32le", "pipe:1",
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
    n = min(reference.size, candidate.size)
    a, b = _normalize(reference[:n]), _normalize(candidate[:n])
    size = 1 << int((2 * n - 1).bit_length())
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)[:2 * n - 1]
    corr = np.concatenate((corr[-(n - 1):], corr[:n]))
    scores = np.abs(corr)
    peak = int(np.argmax(scores))
    lag = peak - (n - 1)
    peak_value = float(scores[peak])
    radius = max(1, sample_rate // 4)
    masked = scores.copy()
    masked[max(0, peak - radius):min(scores.size, peak + radius + 1)] = 0
    runner = float(np.max(masked))
    confidence = min(1.0, max(0.0, peak_value / (runner + 1e-9) - 1.0))
    return lag / sample_rate, confidence


def estimate_offset(reference: np.ndarray, candidate: np.ndarray, sample_rate: int) -> tuple[float, float]:
    if reference.ndim != 2 or candidate.ndim != 2:
        raise ValueError("Audio arrays must be [samples, channels]")
    if reference.shape[1] != candidate.shape[1]:
        raise ValueError("Reference and candidate channel counts differ; refusing to downmix")
    offsets, confidences = [], []
    for channel in range(reference.shape[1]):
        off, conf = _channel_offset(reference[:, channel], candidate[:, channel], sample_rate)
        offsets.append(off)
        confidences.append(conf)
    median = float(np.median(offsets))
    agreement = max(0.0, 1.0 - float(np.std(offsets)) / max(0.05, abs(median) + 0.05))
    return median, min(1.0, float(np.median(confidences)) * agreement)


def analyze(reference: Path, candidate: Path, sample_rate: int = 8000) -> SyncAnalysis:
    ref_duration, cand_duration = _duration(reference), _duration(candidate)
    ref_channels, ref_layout, ref_rate = _audio_shape(reference)
    cand_channels, cand_layout, cand_rate = _audio_shape(candidate)
    if ref_channels != cand_channels:
        raise ValueError(f"Audio channel mismatch: reference={ref_channels}, candidate={cand_channels}; refusing to downmix")

    window = min(60.0, ref_duration, cand_duration)
    starts = [0.0]
    if min(ref_duration, cand_duration) > window * 2.5:
        starts += [max(0.0, min(ref_duration, cand_duration) * 0.45),
                   max(0.0, min(ref_duration, cand_duration) * 0.82)]
    offsets, confs = [], []
    for start in starts:
        ref_pcm = _pcm(reference, sample_rate, ref_channels, start, window)
        cand_pcm = _pcm(candidate, sample_rate, cand_channels, start, window)
        off, conf = estimate_offset(ref_pcm, cand_pcm, sample_rate)
        offsets.append(off)
        confs.append(conf)

    slope = 0.0
    if len(offsets) >= 2:
        x = np.asarray(starts, dtype=np.float64)
        slope = float(np.polyfit(x, np.asarray(offsets), 1)[0])
    drift = cand_duration - ref_duration
    fps_ref, vfr_ref = _video_info(reference)
    fps_cand, vfr_cand = _video_info(candidate)
    confidence = float(np.median(confs))
    if len(offsets) > 1:
        confidence *= max(0.0, 1.0 - min(1.0, float(np.std(offsets)) / 0.25))

    return SyncAnalysis(
        reference_duration=ref_duration, candidate_duration=cand_duration,
        estimated_offset_seconds=float(offsets[0]), confidence=min(1.0, confidence),
        drift_seconds=drift, sample_rate=sample_rate, channels=ref_channels,
        method=f"native-channel PCM FFT multi-window correlation + drift slope + PTS/FPS/VFR metadata; layout={ref_layout or f'{ref_channels}ch'}",
        reference_sample_rate=ref_rate, candidate_sample_rate=cand_rate,
        reference_channels=ref_channels, candidate_channels=cand_channels,
        reference_layout=ref_layout, candidate_layout=cand_layout,
        video_fps_reference=fps_ref, video_fps_candidate=fps_cand,
        vfr_reference=vfr_ref, vfr_candidate=vfr_cand, segment_offsets=tuple(offsets),
    )
