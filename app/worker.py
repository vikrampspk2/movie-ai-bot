from __future__ import annotations

import asyncio
import logging

from telegram import Bot

from .backends import BackendError, backend_selector, dispatch_remote
from .config import settings
from .encode import encode_to_mkv
from .media.probe import probe
from .models import Job, JobStatus, JobType
from .persistence import JobStore
from .queue import queue
from .sync.engine import sync_and_verify
from .upscale import upscale_4k
from .uploaders import upload_to_all

store = JobStore(settings.workspace_root)
log = logging.getLogger("vikky-worker")


async def _retry(fn, attempts: int = 3):
    last = None
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(2 ** attempt)
    raise last


async def _telegram_upload(bot: Bot, job: Job) -> bool:
    if not job.output_path or job.owner_id is None:
        return False

    async def send():
        with job.output_path.open("rb") as handle:
            await bot.send_document(
                chat_id=job.owner_id,
                document=handle,
                caption=f"🎬 Vikky {job.type.value.upper()} complete\n📁 {job.output_path.name}",
            )

    await _retry(send, settings.telegram_max_upload_retries)
    return True


async def _sync_job(job: Job, bot: Bot) -> None:
    if job.source_path is None or job.reference_path is None:
        raise ValueError("SYNC requires both reference and candidate media")
    output = job.source_path.parent / "Sync by Vikky.mkv"
    job.stage, job.progress = "sync_analyzing", 10.0
    verified = await asyncio.to_thread(
        sync_and_verify, job.reference_path, job.source_path, output
    )
    job.stage, job.progress = "sync_verified", 65.0
    job.checkpoint, job.output_path = str(output), output
    job.audio_layout = f"{verified.channels}ch"
    job.stage, job.progress = "external_uploads", 75.0
    job.upload_links = await upload_to_all(output)
    job.stage = "telegram_upload"
    if not await _telegram_upload(bot, job):
        raise RuntimeError("Telegram upload failed; verified output retained")
    await asyncio.to_thread(probe, output)
    job.progress, job.stage = 100.0, "published"


async def _local_job(job: Job, bot: Bot) -> None:
    if job.type is JobType.SYNC:
        await _sync_job(job, bot)
        return

    if job.source_path is None:
        raise ValueError(f"{job.type.value.upper()} requires source media")

    if job.type is JobType.UPSCALE:
        output = job.source_path.parent / "Vikky AI Upscale 4K.mkv"
        job.stage, job.progress, job.backend = "ai_upscaling", 10.0, "local-gpu"
        result = await asyncio.to_thread(
            upscale_4k, job.source_path, output, job.source_path.parent / "upscale-work"
        )
        job.output_path, job.checkpoint, job.progress = output, str(output), 80.0
        job.audio_layout = "preserved"
        job.media_info = result
        job.verified = True
    elif job.type is JobType.ENCODE:
        output = job.source_path.parent / "Vikky encoding.mkv"
        job.stage, job.progress, job.backend = "encoding", 10.0, "local"
        result = await asyncio.to_thread(encode_to_mkv, job.source_path, output, 3.0, 5.0)
        info = await asyncio.to_thread(probe, output)
        job.output_path, job.checkpoint, job.progress = result.output, str(result.output), 80.0
        job.media_info = {
            "size_bytes": result.actual_bytes,
            "codec": result.codec,
            "video_bitrate_kbps": result.video_bitrate_kbps,
            "container": info.container,
            "duration": info.duration,
        }
        job.verified = True
    else:
        raise NotImplementedError(f"{job.type.value} worker is not enabled yet")

    job.stage = "external_uploads"
    job.upload_links = await upload_to_all(job.output_path)
    job.stage = "telegram_upload"
    if not await _telegram_upload(bot, job):
        raise RuntimeError("Telegram upload failed; verified output retained")
    job.progress, job.stage = 100.0, "published"


async def _run_with_failover(job: Job, bot: Bot) -> None:
    excluded: set[str] = set()

    while True:
        backend = backend_selector.select(job, excluded)
        if backend is None:
            await _local_job(job, bot)
            return

        try:
            job.backend = backend.name
            job.stage = f"dispatching_{backend.name}"
            store.save(job)
            result = await dispatch_remote(backend, job)
            job.checkpoint = result.get("job_id", job.checkpoint)
            job.stage = f"remote_accepted_{backend.name}"
            store.save(job)
            # A provider-specific result/poll adapter must be supplied before
            # treating a remote acceptance as completed.
            raise BackendError(
                f"{backend.name} accepted the job but has no result adapter configured"
            )
        except BackendError as exc:
            excluded.add(backend.name)
            job.retry_count += 1
            job.error = str(exc)
            store.save(job)


async def process_job(job: Job, bot: Bot) -> None:
    job.status, job.backend = JobStatus.RUNNING, "selecting"
    store.save(job)
    try:
        await _run_with_failover(job, bot)
        job.status, job.stage = JobStatus.SUCCEEDED, "completed"
        store.save(job)
    except asyncio.CancelledError:
        job.status, job.stage = JobStatus.CANCELLED, "cancelled"
        store.save(job)
        raise
    except Exception as exc:
        job.status, job.stage, job.error = JobStatus.FAILED, "failed", str(exc)
        store.save(job)
        log.exception("Job %s failed", job.id)


async def worker_loop(bot: Bot) -> None:
    while True:
        job = await queue.next()
        if job is None:
            await asyncio.sleep(0.5)
            continue
        await process_job(job, bot)
