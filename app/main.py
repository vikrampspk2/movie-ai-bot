import asyncio
import logging

from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .backends import backend_selector
from .config import settings
from .models import Job, JobType
from .queue import queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vikky-bot")

api = FastAPI(title="Vikky Movie AI Bot", version="0.1.0")


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "vikky-movie-ai-bot"}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎬 Vikky Movie AI Bot\n\n"
        "Foundation is online.\n"
        "/status — system status\n"
        "/queue — queue snapshot\n"
        "/help — commands"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start\n/status\n/queue\n/cancel <job_id>\n\n"
        "Processing commands will be enabled as each verified media module is added."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs = await queue.snapshot()
    running = next((j for j in jobs if j.status.value == "running"), None)
    selected = backend_selector.select(running) if running else None
    text = "🟢 Vikky status\n"
    text += f"Jobs: {len(jobs)}\n"
    text += f"Current: {running.id if running else 'idle'}\n"
    text += f"Backend: {selected.name if selected else 'not configured'}"
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
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("cancel", cancel))
    log.info("Vikky bot foundation starting")
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
