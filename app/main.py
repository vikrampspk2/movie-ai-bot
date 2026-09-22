import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .backends import backend_selector
from .config import settings
from .models import Job, JobType
from .queue import queue
from .worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vikky-bot")

api = FastAPI(title="Vikky Movie AI Bot", version="0.1.0")
MEDIA_RE = re.compile(r"\.(mkv|mp4|m4v|mov|webm|avi|ts|m2ts|zip|iso)$", re.I)


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "vikky-movie-ai-bot"}


def _workspace(user_id: int) -> Path:
    path = settings.workspace_root / str(user_id) / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_type(context: ContextTypes.DEFAULT_TYPE) -> JobType | None:
    value = context.user_data.get("pending_job_type")
    return JobType(value) if value else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎬 Vikky Movie AI Bot\n\n"
        "⚡ Fast intake + safe queue foundation online.\n"
        "/sync — send media for synchronization\n"
        "/upscale — send media for AI 4K\n"
        "/encode — send media for encoding\n"
        "/status — live job status\n"
        "/queue — queue snapshot\n"
        "/cancel <job_id> — cancel a queued job\n"
        "/help — commands"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/sync, /upscale, /encode → choose a job, then send MKV/MP4/ZIP/ISO\n"
        "/status → current job\n/queue → queue\n/cancel <job_id> → cancel\n\n"
        "Direct media URLs and accelerated aria2c intake are reserved for the next intake worker."
    )


async def choose_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text.split()[0].lstrip("/").lower()
    job_type = JobType(command)
    context.user_data["pending_job_type"] = job_type.value
    context.user_data["sync_files"] = []
    await update.message.reply_text(
        f"✅ {job_type.value.upper()} selected.\n"
        "Now send the MKV/MP4/ZIP/ISO file.\n"
        "The bot will create a queued job and never overwrite the original."
    )


async def receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    job_type = _job_type(context)
    if not job_type or not update.message:
        return

    document = update.message.document
    video = update.message.video
    media = document or video
    if media is None:
        return

    name = Path(getattr(media, "file_name", None) or f"telegram_{uuid4().hex}.bin").name
    if not MEDIA_RE.search(name):
        await update.message.reply_text("❌ Unsupported media type. Use MKV/MP4/M4V/MOV/WEBM/AVI/TS/M2TS/ZIP/ISO.")
        return

    workspace = _workspace(update.effective_user.id)
    destination = workspace / name
    tg_file = await context.bot.get_file(media.file_id)
    await tg_file.download_to_drive(custom_path=destination)

    if job_type == JobType.SYNC:
        files = context.user_data.setdefault("sync_files", [])
        files.append(destination)
        if len(files) == 1:
            await update.message.reply_text("📌 SYNC reference/candidate mode: send the second media file. I will not self-sync one file.")
            return
        job = Job(type=job_type, source_name=files[1].name, source_path=files[1], reference_path=files[0], owner_id=update.effective_user.id)
        context.user_data.pop("pending_job_type", None)
        context.user_data.pop("sync_files", None)
    else:
        job = Job(type=job_type, source_name=name, source_path=destination, owner_id=update.effective_user.id)
        context.user_data.pop("pending_job_type", None)
    await queue.add(job)

    snapshot = await queue.snapshot()
    position = sum(1 for item in snapshot if item.status.value == "queued" and item.id != job.id)
    await update.message.reply_text(
        f"📥 Accepted: {name}\n"
        f"🆔 Job: {job.id}\n"
        f"📋 Queue position: {position + 1}\n"
        f"⚡ Status: queued\n\n"
        "Worker will process, verify, and publish the result."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs = await queue.snapshot()
    running = next((j for j in jobs if j.status.value == "running"), None)
    selected = backend_selector.select(running) if running else None
    text = "🟢 Vikky status\n"
    text += f"Jobs: {len(jobs)}\n"
    text += f"Current: {running.id if running else 'idle'}\n"
    text += f"Backend: {selected.name if selected else 'not configured'}"
    if running:
        text += f"\nStage: {running.stage}\nProgress: {running.progress:.0f}%"
    await update.message.reply_text(text)


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs = await queue.snapshot()
    if not jobs:
        await update.message.reply_text("📭 Queue empty")
        return
    lines = [f"{j.id} · {j.type.value} · {j.status.value} · {j.progress:.0f}%" for j in jobs[-10:]]
    await update.message.reply_text("📋 Queue\n" + "\n".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /cancel <job_id>")
        return
    ok = await queue.cancel(context.args[0])
    await update.message.reply_text("✅ Cancelled" if ok else "❌ Job not found or already finished")


async def run() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    settings.workspace_root.mkdir(parents=True, exist_ok=True)

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("sync", choose_job))
    application.add_handler(CommandHandler("upscale", choose_job))
    application.add_handler(CommandHandler("encode", choose_job))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO, receive_media))

    asyncio.create_task(worker_loop())
    log.info("Vikky bot intake starting")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
