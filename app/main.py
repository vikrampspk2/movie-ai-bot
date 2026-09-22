from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from .models import Job, JobStatus


class JobQueue:
    """Async in-memory queue with bounded concurrency for the bot process."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._pending: deque[str] = deque()
        self._lock = asyncio.Lock()

    async def add(self, job: Job) -> Job:
        async with self._lock:
            self._jobs[job.id] = job
            self._pending.append(job.id)
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def next(self) -> Job | None:
        async with self._lock:
            while self._pending:
                job_id = self._pending.popleft()
                job = self._jobs.get(job_id)
                if job and job.status == JobStatus.QUEUED:
                    return job
            return None

    async def requeue(self, job_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == JobStatus.QUEUED:
                self._pending.append(job_id)

    async def cancel(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return False
            job.status = JobStatus.CANCELLED
            job.stage = "cancelled"
            return True

    async def snapshot(self) -> list[Job]:
        async with self._lock:
            return list(self._jobs.values())


queue = JobQueue()
