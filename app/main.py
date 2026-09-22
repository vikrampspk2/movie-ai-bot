from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import settings
from .models import JobStatus
from .queue import queue
from .worker import worker_loop

log = logging.getLogger("vikky-bot")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎬 Vikky Movie AI Bot is online.\n\n"
        "/help — commands\n"
        "/status — current jobs\n"
        "/queue — queue\n"
        "/cancel <job_id> — cancel a queued job"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/start\n/status\n/queue\n/cancel <job_id>\n"
        "/sync — media sync workflow\n/upscale — AI 4K workflow\n/encode — MKV encode workflow"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs = await queue.snapshot()
    if not jobs:
        await update.message.reply_text("✅ Queue empty. Bot is healthy.")
        return
    running = [j for j in jobs if j.status is JobStatus.RUNNING]
    queued = [j for j in jobs if j.status is JobStatus.QUEUED]
    await update.message.reply_text(
        f"📊 Jobs: {len(jobs)}\n"
        f"▶️ Running: {len(running)}\n"
        f"⏳ Queued: {len(queued)}"
    )


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs = await queue.snapshot()
    queued = [j for j in jobs if j.status is JobStatus.QUEUED]
    if not queued:
        await update.message.reply_text("📭 Queue empty.")
        return
    lines = [f"#{i + 1} {job.id} — {job.type.value} — {job.stage}" for i, job in enumerate(queued)]
    await update.message.reply_text("\n".join(lines[:20]))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /cancel <job_id>")
        return
    ok = await queue.cancel(context.args[0])
    await update.message.reply_text("🛑 Cancelled." if ok else "❌ Job not found or already finished.")


async def unsupported_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📥 Send the media file first. The processing worker will attach it to the requested workflow."
    )


async def post_init(application: Application) -> None:
    application.create_task(worker_loop(application.bot))


def build_application() -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("sync", unsupported_workflow))
    application.add_handler(CommandHandler("upscale", unsupported_workflow))
    application.add_handler(CommandHandler("encode", unsupported_workflow))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
