import asyncio
from collections import deque

from .models import Job, JobStatus


class JobQueue:
    """Small in-process queue for the first foundation stage.

    Persistent queue/checkpoint storage will be added before production deployment.
    """

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
                job = self._jobs[self._pending.popleft()]
                if job.status == JobStatus.QUEUED:
                    return job
            return None

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
