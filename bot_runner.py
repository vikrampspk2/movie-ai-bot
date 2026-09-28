from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable

from pyrogram import Client, filters
from pyrogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.encode import EncodeError, encode_to_mkv
from app.media.downloader import DownloadError, download_url
from app.uploaders import upload_to_all
from app.upscale import UpscaleError, upscale_4k

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vikky-bot")

API_ID = int(os.getenv("PYROGRAM_API_ID") or os.getenv("API_ID") or "0")
API_HASH = os.getenv("PYROGRAM_API_HASH") or os.getenv("API_HASH") or ""
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Telegram credentials levu. PYROGRAM_API_ID, PYROGRAM_API_HASH "
        "mariyu TELEGRAM_BOT_TOKEN set cheyyali."
    )

app = Client(
    "vikky_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

URL_RE = re.compile(r"https?://[^\s<>\"]+")
MAX_ACTIVE_JOBS = 2
GPU_RUNNING = False
CPU_RUNNING = False
job_queue: deque["Job"] = deque()
jobs: dict[str, "Job"] = {}
chat_jobs: dict[int, str] = {}


@dataclass
class Job:
    id: str
    chat_id: int
    url: str
    kind: str
    status: str = "queued"
    stage: str = "queued"
    work_dir: Path | None = None
    source: Path | None = None
    output: Path | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    heartbeat: asyncio.Task | None = field(default=None, repr=False)
    created_at: float = field(default_factory=monotonic)
    cancelled: bool = False
    control_message_id: int | None = None


def find_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,);]}>\"'") if match else None


def keyboard_for(job: Job, processing: bool = False) -> InlineKeyboardMarkup:
    if processing:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 Cancel Task", callback_data=f"cancel:{job.id}")]]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 4K Upscale", callback_data=f"choose:upscale:{job.id}"),
                InlineKeyboardButton("📦 Fast Encode", callback_data=f"choose:encode:{job.id}"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{job.id}")],
        ]
    )


async def say(chat_id: int, text: str, reply_markup=None) -> Message:
    return await app.send_message(chat_id, text, reply_markup=reply_markup)


async def edit_control(job: Job, text: str, processing: bool = False) -> None:
    if not job.control_message_id:
        return
    try:
        await app.edit_message_text(
            job.chat_id,
            job.control_message_id,
            text,
            reply_markup=keyboard_for(job, processing),
        )
    except Exception:
        log.debug("Control message edit failed", exc_info=True)


def cancel_job(job: Job) -> None:
    job.cancelled = True
    if job.task and not job.task.done():
        job.task.cancel()


async def cleanup_job(job: Job) -> None:
    if job.heartbeat and not job.heartbeat.done():
        job.heartbeat.cancel()
    if job.work_dir:
        await asyncio.to_thread(shutil.rmtree, job.work_dir, True)
    jobs.pop(job.id, None)
    if chat_jobs.get(job.chat_id) == job.id:
        chat_jobs.pop(job.chat_id, None)


async def heartbeat(job: Job) -> None:
    while True:
        await asyncio.sleep(20)
        if job.cancelled or job.status not in {"running", "processing"}:
            return
        stage = {
            "download": "movie download avuthondi",
            "encode": "movie encoding avuthondi",
            "upscale": "4K AI upscale avuthondi",
            "upload": "output upload avuthondi",
        }.get(job.stage, "movie processing avuthondi")
        await say(
            job.chat_id,
            f"⏳ {stage}.\n"
            "Background lo pani continue avuthondi.",
        )


async def acquire_slot(job: Job) -> str:
    global GPU_RUNNING, CPU_RUNNING
    # Upscale uses the GPU. Encode uses the CPU slot.
    # Required policy: GPU work can coexist with one CPU encode, but once
    # a CPU task is running, new tasks wait in the queue.
    if job.kind == "upscale":
        while GPU_RUNNING or CPU_RUNNING:
            await asyncio.sleep(0.5)
        GPU_RUNNING = True
        return "gpu"

    while CPU_RUNNING or (GPU_RUNNING and job.kind != "encode"):
        await asyncio.sleep(0.5)
    if CPU_RUNNING:
        while CPU_RUNNING:
            await asyncio.sleep(0.5)
    CPU_RUNNING = True
    return "cpu"


async def release_slot(slot: str) -> None:
    global GPU_RUNNING, CPU_RUNNING
    if slot == "gpu":
        GPU_RUNNING = False
    else:
        CPU_RUNNING = False


async def run_job(job: Job) -> None:
    slot = await acquire_slot(job)
    job.status = "running"
    job.stage = "download"
    try:
        await edit_control(
            job,
            "🚀 Job start ayyindi.\n"
            "📥 Movie link nundi download chestunnanu.",
            processing=True,
        )
        job.source = await download_url(job.url, job.work_dir)
        if job.cancelled:
            raise asyncio.CancelledError

        if job.kind == "upscale":
            job.stage = "upscale"
            await say(
                job.chat_id,
                "🎬 Download complete ayyindi.\n"
                "✨ Ippudu 4K AI upscale start chestunnanu.",
            )
            job.output = job.work_dir / "Vikky AI Upscale 4K.mkv"
            await asyncio.to_thread(
                upscale_4k,
                job.source,
                job.output,
                job.work_dir / "upscale-work",
            )
        else:
            job.stage = "encode"
            await say(
                job.chat_id,
                "🎬 Download complete ayyindi.\n"
                "⚙️ Ippudu fast encode start chestunnanu.",
            )
            job.output = job.work_dir / "Vikky encoding.mkv"
            await asyncio.to_thread(
                encode_to_mkv,
                job.source,
                job.output,
                3.0,
                5.0,
            )

        if job.cancelled:
            raise asyncio.CancelledError

        job.stage = "upload"
        await say(
            job.chat_id,
            "✅ Processing complete ayyindi.\n"
            "☁️ Output links prepare chestunnanu.",
        )
        links = await upload_to_all(job.output)
        good = {k: v for k, v in links.items() if v and not str(v).startswith("ERROR:")}
        if not good:
            raise RuntimeError("Upload hosts nundi link raledu.")

        job.status = "completed"
        await say(
            job.chat_id,
            "🎉 Mee movie ready ayyindi!\n\n" +
            "\n".join(f"🔗 {name}: {link}" for name, link in good.items()),
        )
    except asyncio.CancelledError:
        job.status = "cancelled"
        await say(job.chat_id, "🛑 Mee task cancel ayyindi.\nTemporary files clean chestunnanu.")
    except (DownloadError, EncodeError, UpscaleError) as exc:
        job.status = "failed"
        await say(job.chat_id, f"❌ Process lo problem vachindi.\nReason: {exc}")
        log.exception("Job %s failed", job.id)
    except Exception:
        job.status = "failed"
        await say(
            job.chat_id,
            "❌ Movie process complete cheyyalekapoyanu.\n"
            "Konchem sepu tarvata malli try cheyyandi.",
        )
        log.exception("Job %s failed", job.id)
    finally:
        await release_slot(slot)
        if job.heartbeat and not job.heartbeat.done():
            job.heartbeat.cancel()
        await cleanup_job(job)
        await scheduler()


async def scheduler() -> None:
    # Serialized scheduler prevents two queue decisions racing each other.
    while job_queue:
        next_job = job_queue[0]
        if next_job.cancelled:
            job_queue.popleft()
            await cleanup_job(next_job)
            continue

        # GPU upscale and CPU encode may run together. A CPU task never
        # starts while another CPU task is active.
        if next_job.kind == "upscale":
            if GPU_RUNNING or CPU_RUNNING:
                return
        else:
            if CPU_RUNNING:
                return

        job_queue.popleft()
        next_job.task = asyncio.create_task(run_job(next_job))
        return


async def enqueue(chat_id: int, url: str, kind: str) -> Job:
    job = Job(
        id=os.urandom(6).hex(),
        chat_id=chat_id,
        url=url,
        kind=kind,
        work_dir=Path(tempfile.mkdtemp(prefix="vikky-")),
    )
    jobs[job.id] = job
    chat_jobs[chat_id] = job.id
    job_queue.append(job)
    await scheduler()
    return job


@app.on_callback_query()
async def callback_handler(_, query: CallbackQuery) -> None:
    data = query.data or ""
    parts = data.split(":")
    job = jobs.get(parts[-1]) if len(parts) >= 2 else None

    if not job:
        await query.answer("Ee task ippudu active ga ledu.", show_alert=True)
        return

    if data.startswith("cancel:"):
        cancel_job(job)
        if job in job_queue:
            try:
                job_queue.remove(job)
            except ValueError:
                pass
            await cleanup_job(job)
            await query.message.edit_text("❌ Task cancel ayyindi.\nTemporary files clean chesanu.")
            await query.answer("Task cancel ayyindi.")
            return
        await query.answer("Task cancel chestunnanu.")
        return

    if data.startswith("choose:"):
        if job.status != "queued":
            await query.answer("Ee task already process lo undi.", show_alert=True)
            return
        kind = parts[1]
        job.kind = kind
        await query.message.edit_text(
            "🎯 Mee option select ayyindi.\n"
            f"{'🎬 4K AI upscale' if kind == 'upscale' else '📦 Fast encode'} start avuthundi.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("⬅️ Back", callback_data=f"back:{job.id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{job.id}"),
                ]]
            ),
        )
        await scheduler()
        await query.answer("Option select ayyindi.")
        return

    if data.startswith("back:"):
        if job.status != "queued":
            await query.answer("Processing already start ayyindi.", show_alert=True)
            return
        await query.message.edit_text(
            "🎬 Mee movie ki em cheyyali?\n"
            "Oka option select cheyyandi.",
            reply_markup=keyboard_for(job),
        )
        await query.answer()
        return


@app.on_message(filters.private & filters.command("start"))
async def start_handler(_, message: Message) -> None:
    await message.reply_text(
        "Namaskaram! Ee bot lo movie processing automated ga jarugutundi.\n\n"
        "Movie link pampiste, mundu processing option select cheyyachu.\n"
        "Tarvata download, processing mariyu upload background lo automatic ga jarugutayi.\n\n"
        "/help - Bot ni ela use cheyalo\n"
        "/status - Mee current job details"
    )


@app.on_message(filters.private & filters.command("help"))
async def help_handler(_, message: Message) -> None:
    await message.reply_text(
        "📖 Bot ni use cheyadam ila:\n\n"
        "1. Movie direct link pampandi.\n"
        "2. Link message automatic ga delete avuthundi.\n"
        "3. 🎬 4K Upscale leda 📦 Fast Encode select cheyyandi.\n"
        "4. Processing background lo start avuthundi.\n"
        "5. Process madhyalo live progress messages vastayi.\n"
        "6. 🛑 Cancel Task button tho running task ni aapavachu.\n"
        "7. Task complete ayyaka download links vastayi.\n\n"
        "Queue busy unte mee task waitlist lo untundi; slot free ayyaka "
        "automatic ga start avuthundi.\n\n"
        "/encode <link> - Fast encode direct ga start cheyyadaniki\n"
        "/upscale <link> - 4K AI upscale direct ga start cheyyadaniki\n"
        "/status - Current job details chudataniki"
    )


@app.on_message(filters.private & filters.command("status"))
async def status_handler(_, message: Message) -> None:
    job_id = chat_jobs.get(message.chat.id)
    job = jobs.get(job_id) if job_id else None
    if not job:
        await message.reply_text(
            "Mee kosam active leda queued job emi ledu.\n"
            "Movie link pampandi."
        )
        return

    if job.status == "queued":
        position = list(job_queue).index(job) + 1 if job in job_queue else 1
        await message.reply_text(
            f"📊 Mee job status\n\n"
            f"🆔 Job: {job.id}\n"
            f"⏳ Queue lo undi.\n"
            f"📍 Mee position: {position}\n"
            "Slot free ayyaka automatic ga start avuthundi."
        )
        return

    detail = {
        "download": "Movie download avuthondi.",
        "encode": "Movie encoding avuthondi.",
        "upscale": "4K AI upscale avuthondi.",
        "upload": "Output links upload avuthunnayi.",
    }.get(job.stage, "Movie processing avuthondi.")
    await message.reply_text(
        f"📊 Mee job status\n\n"
        f"🆔 Job: {job.id}\n"
        f"⚙️ Stage: {job.stage}\n"
        f"ℹ️ {detail}\n\n"
        "Process background lo continue avuthondi."
    )


async def direct_command(message: Message, kind: str) -> None:
    url = find_url(" ".join(message.command[1:]) if message.command else "")
    if not url:
        await message.reply_text(
            f"/{kind} <movie link> ila pampandi."
        )
        return
    if message.chat.id in chat_jobs:
        await message.reply_text(
            "Mee previous task inka active ga undi.\n"
            "/status tho details chudandi."
        )
        return
    try:
        await message.delete()
    except Exception:
        pass
    job = await enqueue(message.chat.id, url, kind)
    msg = await say(
        message.chat.id,
        f"🚀 {kind} task receive ayyindi.\n"
        "Queue mariyu processing slot check chestunnanu.",
        keyboard_for(job),
    )
    job.control_message_id = msg.id


@app.on_message(filters.private & filters.command("encode"))
async def encode_command(_, message: Message) -> None:
    await direct_command(message, "encode")


@app.on_message(filters.private & filters.command("upscale"))
async def upscale_command(_, message: Message) -> None:
    await direct_command(message, "upscale")


@app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "status", "encode", "upscale"]))
async def link_handler(_, message: Message) -> None:
    url = find_url(message.text or "")
    if not url:
        await message.reply_text(
            "Movie direct download link pampandi.\n"
            "/help tho full guide chudandi."
        )
        return
    if message.chat.id in chat_jobs:
        await message.reply_text(
            "Mee previous task inka active ga undi.\n"
            "/status tho details chudandi."
        )
        return

    try:
        await message.delete()
    except Exception:
        log.debug("Link message delete cheyyalekapoyanu.", exc_info=True)

    job = await enqueue(message.chat.id, url, "encode")
    msg = await say(
        message.chat.id,
        "🔗 Link receive ayyindi.\n"
        "🎬 Mee processing option select cheyyandi.",
        keyboard_for(job),
    )
    job.control_message_id = msg.id


async def main() -> None:
    log.info("Pyrogram polling bot start chestunnanu...")
    await app.start()
    try:
        # Replace Telegram's default command list so stale menu commands are removed.
        await app.delete_bot_commands()
        await app.set_bot_commands(
            [
                BotCommand("start", "Bot start cheyyadaniki"),
                BotCommand("help", "Bot usage guide"),
                BotCommand("status", "Current job status"),
            ]
        )
        me = await app.get_me()
        log.info("Bot started successfully: @%s (%s)", me.username, me.id)
        log.info("Telegram command menu refreshed: /start /help /status")
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info("Bot main task cancelled.")
        raise
    except Exception:
        log.exception("Bot runtime error.")
        raise
    finally:
        try:
            await app.stop()
        except Exception:
            log.exception("Bot shutdown error.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
