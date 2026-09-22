from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import time
from uuid import uuid4


class JobType(StrEnum):
    SYNC = "sync"
    UPSCALE = "upscale"
    ENCODE = "encode"


class JobStatus(StrEnum):
    QUEUED = "queued"
    WAITING_INPUT = "waiting_input"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Job:
    type: JobType
    source_name: str
    source_path: Path | None = None
    reference_path: Path | None = None
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    status: JobStatus = JobStatus.QUEUED
    stage: str = "queued"
    progress: float = 0.0
    backend: str = "pending"
    error: str | None = None
    created_at: float = field(default_factory=time)
    checkpoint: str | None = None
    audio_layout: str | None = None
    output_path: Path | None = None
    owner_id: int | None = None
    upload_links: dict[str, str] = field(default_factory=dict)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time() - self.created_at)
