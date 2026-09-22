from __future__ import annotations
import asyncio
import logging
from telegram import Bot
from .media.probe import probe
from .models import Job, JobStatus, JobType
from .queue import queue
from .sync.engine import sync_and_verify
from .encode import encode_to_mkv
from .uploaders import upload_to_all
log = logging.getLogger("vikky-worker")

async def _retry(fn, attempts: int = 3):
    last = None
    for attempt in range(attempts):
        try: return await fn()
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts: await asyncio.sleep(2 ** attempt)
    raise last

async def _telegram_upload(bot: Bot, job: Job) -> bool:
    if not job.output_path or job.owner_id is None: return False
    async def send():
        with job.output_path.open("rb") as handle:
            await bot.send_document(chat_id=job.owner_id, document=handle,
                                    caption=f"🎬 Vikky {job.type.value.upper()} complete\n📁 {job.output_path.name}")
    await _retry(send)
    return True

async def _sync_job(job: Job, bot: Bot) -> None:
    if job.source_path is None or job.reference_path is None: raise ValueError("SYNC requires both reference and candidate media")
    output = job.source_path.parent / "Sync by Vikky.mkv"
    job.stage, job.progress = "sync_analyzing", 10.0
    verified = await asyncio.to_thread(sync_and_verify, job.reference_path, job.source_path, output)
    job.stage, job.progress = "sync_verified", 65.0
    job.checkpoint, job.output_path = str(output), output
    job.audio_layout = f"{verified.channels}ch"
    job.stage, job.progress = "external_uploads", 75.0
    job.upload_links = await upload_to_all(output)
    job.stage = "telegram_upload"
    if not await _telegram_upload(bot, job): raise RuntimeError("Telegram upload failed; verified output retained")
    await asyncio.to_thread(probe, output)
    job.progress = 100.0
    job.stage = "published"

async def process_job(job: Job, bot: Bot) -> None:
    job.status, job.backend = JobStatus.RUNNING, "cpu"
    try:
        if job.type == JobType.SYNC:
            await _sync_job(job, bot)
        elif job.type == JobType.ENCODE:
            if job.source_path is None: raise ValueError("ENCODE requires source media")
            output = job.source_path.parent / "Vikky encoding.mkv"
            job.stage, job.progress = "encoding", 10.0
            result = await asyncio.to_thread(encode_to_mkv, job.source_path, output, 3.0, 5.0)
            job.output_path, job.checkpoint, job.progress = result.output, str(result.output), 80.0
            info = await asyncio.to_thread(probe, output)
            job.media_info = {"size_bytes": result.actual_bytes, "codec": result.codec, "video_bitrate_kbps": result.video_bitrate_kbps, "container": info.container, "duration": info.duration}
            job.verified = True
            job.stage = "external_uploads"
            job.upload_links = await upload_to_all(output)
            job.stage = "telegram_upload"
            if not await _telegram_upload(bot, job): raise RuntimeError("Telegram upload failed; verified output retained")
            job.progress, job.stage = 100.0, "published"
        else:
            raise NotImplementedError(f"{job.type.value} worker is not enabled yet")
        job.status, job.stage = JobStatus.SUCCEEDED, "completed"
    except asyncio.CancelledError:
        job.status, job.stage = JobStatus.CANCELLED, "cancelled"; raise
    except Exception as exc:
        job.status, job.stage, job.error = JobStatus.FAILED, "failed", str(exc)
        log.exception("Job %s failed", job.id)

async def worker_loop(bot: Bot) -> None:
    while True:
        job = await queue.next()
        if job is None: await asyncio.sleep(0.5); continue
        await process_job(job, bot)
