from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .models import Job, JobStatus, JobType
from .queue import queue
from .uploaders import upload_to_all
from .sync.engine import SyncVerificationError, sync_and_verify

log = logging.getLogger("vikky-worker")


async def _sync_job(job: Job) -> None:
    if job.source_path is None or job.reference_path is None:
        raise ValueError("SYNC requires both reference and candidate media")

    output = job.source_path.parent / "Sync by Vikky.mkv"
    job.stage = "sync_analyzing"
    job.progress = 10.0
    verified = await asyncio.to_thread(sync_and_verify, job.reference_path, job.source_path, output)
    job.stage = "sync_verified"
    job.progress = 70.0
    job.checkpoint = str(output)
    job.output_path = output
    job.audio_layout = f"{verified.channels}ch"
    job.stage = "uploading"
    job.upload_links = await upload_to_all(output)
    job.progress = 100.0
    job.checkpoint = str(output)


async def process_job(job: Job) -> None:
    job.status = JobStatus.RUNNING
    job.backend = "cpu"
    try:
        if job.type == JobType.SYNC:
            await _sync_job(job)
        else:
            raise NotImplementedError(f"{job.type.value} worker is not enabled yet")
        job.status = JobStatus.SUCCEEDED
        job.stage = "completed"
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.stage = "cancelled"
        raise
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.stage = "failed"
        job.error = str(exc)
        log.exception("Job %s failed", job.id)


async def worker_loop() -> None:
    while True:
        job = await queue.next()
        if job is None:
            await asyncio.sleep(0.5)
            continue
        await process_job(job)
