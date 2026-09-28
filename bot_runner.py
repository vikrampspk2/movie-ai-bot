from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message

from app.encode import EncodeError, encode_to_mkv
from app.media.downloader import DownloadError, download_url
from app.uploaders import upload_to_all

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vikky-bot")

API_ID = int(os.getenv("PYROGRAM_API_ID") or os.getenv("API_ID") or "0")
API_HASH = os.getenv("PYROGRAM_API_HASH") or os.getenv("API_HASH") or ""
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
DEFAULT_JOB_TYPE = os.getenv("DEFAULT_JOB_TYPE", "encode").lower()

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Telegram credentials levu. PYROGRAM_API_ID, PYROGRAM_API_HASH "
        "mariyu TELEGRAM_BOT_TOKEN set cheyyali."
    )

if DEFAULT_JOB_TYPE not in {"encode", "upscale"}:
    DEFAULT_JOB_TYPE = "encode"

app = Client(
    "vikky_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

URL_RE = re.compile(r"https?://[^\s<>\"]+")
active_jobs: dict[int, dict[str, object]] = {}


def find_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,);]}>\"'")


async def send_status(chat_id: int, text: str) -> Message:
    return await app.send_message(chat_id, text)


async def job_heartbeat(chat_id: int, job_id: str) -> None:
    while True:
        await asyncio.sleep(20)
        job = active_jobs.get(chat_id)
        if not job or job.get("id") != job_id:
            return
        stage = str(job.get("stage", "processing"))
        await send_status(
            chat_id,
            f"⏳ Ippudu {stage} stage lo pani jarugutondi.\n"
            "Konchem samayam padutundi; process background lo continue avuthundi.",
        )


async def process_link(chat_id: int, url: str, job_id: str) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix=f"vikky-{job_id}-"))
    job = active_jobs[chat_id]

    heartbeat = asyncio.create_task(job_heartbeat(chat_id, job_id))
    try:
        job["stage"] = "download"
        await send_status(
            chat_id,
            "🔗 Link dorikindi.\n"
            "📥 Movie ni fast ga download cheyyadam start chesanu.",
        )

        source = await download_url(url, work_dir)
        job["source"] = str(source)
        job["stage"] = "processing"

        await send_status(
            chat_id,
            "✅ Download complete.\n"
            "🎬 Ippudu movie processing start ayyindi.",
        )

        if DEFAULT_JOB_TYPE == "upscale":
            # Real AI upscale needs the configured GPU backend. Do not fake completion.
            raise RuntimeError(
                "AI upscale backend inka configure cheyyaledu. "
                "DEFAULT_JOB_TYPE=encode tho encoding automatic ga run cheyyandi."
            )

        output = work_dir / "Vikky encoding.mkv"
        job["stage"] = "encoding"

        await send_status(
            chat_id,
            "⚙️ Encoding start ayyindi.\n"
            "🎯 Target file size 3-5 GB range lo prepare chestunnanu.",
        )

        result = await asyncio.to_thread(
            encode_to_mkv,
            source,
            output,
            3.0,
            5.0,
        )

        job["stage"] = "upload"
        await send_status(
            chat_id,
            "✅ Encoding complete ayyindi.\n"
            "☁️ Output ni upload hosts ki pampistunnanu.",
        )

        links = await upload_to_all(output)
        good_links = {
            name: link
            for name, link in links.items()
            if link and not str(link).startswith("ERROR:")
        }

        if not good_links:
            raise RuntimeError("Upload hosts nundi usable link raledu.")

        job["stage"] = "completed"
        job["links"] = good_links
        job["size_gb"] = round(result.actual_bytes / 1024**3, 2)

        lines = [
            "🎉 Movie processing complete ayyindi!",
            f"📦 Final size: {job['size_gb']} GB",
            "",
            "🔗 Download links:",
        ]
        for name, link in good_links.items():
            lines.append(f"{name}: {link}")

        await send_status(chat_id, "\n".join(lines))
    except (DownloadError, EncodeError) as exc:
        job["stage"] = "failed"
        job["error"] = str(exc)
        await send_status(
            chat_id,
            f"❌ Process lo problem vachindi.\n"
            f"Reason: {exc}",
        )
        log.exception("Job %s failed", job_id)
    except Exception as exc:
        job["stage"] = "failed"
        job["error"] = str(exc)
        await send_status(
            chat_id,
            "❌ Processing complete cheyyalekapoyanu.\n"
            "Konchem sepu tarvata malli link pampandi.",
        )
        log.exception("Job %s failed", job_id)
    finally:
        heartbeat.cancel()
        active_jobs.pop(chat_id, None)
        try:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


@app.on_message(filters.private & filters.command("start"))
async def start_handler(_, message: Message) -> None:
    await message.reply_text(
        "Namaskaram! Ee bot lo movie processing automated ga jarugutundi.\n\n"
        "Movie link pampiste, message ni automatic ga delete chesi "
        "background lo download mariyu processing start chestanu.\n\n"
        "Encoding tarvata output links ikkade pampistanu.\n"
        "/help - Bot ela use cheyalo telusukondi\n"
        "/status - Current job details chudandi"
    )


@app.on_message(filters.private & filters.command("help"))
async def help_handler(_, message: Message) -> None:
    await message.reply_text(
        "📖 Bot ni use cheyadam chala simple.\n\n"
        "1. Mee movie direct download link ni pampandi.\n"
        "2. Link message ni bot automatic ga delete chestundi.\n"
        "3. Background lo movie download start avuthundi.\n"
        "4. Download complete ayyaka encoding automatic ga start avuthundi.\n"
        "5. Process madhyalo live status messages vastayi.\n"
        "6. Complete ayyaka available download links ikkade vastayi.\n\n"
        "⚙️ Default processing: movie encoding.\n"
        "📦 Encoding target: 3-5 GB range.\n\n"
        "/start - Bot ni start cheyadaniki\n"
        "/help - Ee guide kosam\n"
        "/status - Current job details kosam\n\n"
        "Link pampinappudu bot ni wait cheyyakunda background lo "
        "process continue chestundi."
    )


@app.on_message(filters.private & filters.command("status"))
async def status_handler(_, message: Message) -> None:
    job = active_jobs.get(message.chat.id)
    if not job:
        await message.reply_text(
            "Ippudu mee kosam active job emi ledu.\n"
            "Movie link pampiste automatic ga process start avuthundi."
        )
        return

    stage = str(job.get("stage", "unknown"))
    if stage == "download":
        detail = "Movie download avuthondi."
    elif stage == "processing":
        detail = "Movie processing avuthondi."
    elif stage == "encoding":
        detail = "Movie encoding avuthondi."
    elif stage == "upload":
        detail = "Output upload avuthondi."
    else:
        detail = "Job background lo run avuthondi."

    await message.reply_text(
        f"📊 Current job status\n\n"
        f"🆔 Job: {job['id']}\n"
        f"⚙️ Stage: {stage}\n"
        f"ℹ️ {detail}\n\n"
        "Process complete ayyaka final links automatic ga vastayi."
    )


@app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "status"]))
async def link_handler(_, message: Message) -> None:
    url = find_url(message.text or "")
    if not url:
        await message.reply_text(
            "Movie direct download link pampandi.\n"
            "/help - Ela use cheyalo chudandi."
        )
        return

    if message.chat.id in active_jobs:
        await message.reply_text(
            "Mee previous movie ippatiki process avuthondi.\n"
            "/status - Current progress chudandi."
        )
        return

    job_id = os.urandom(6).hex()
    active_jobs[message.chat.id] = {
        "id": job_id,
        "stage": "queued",
        "url": url,
    }

    try:
        await message.delete()
    except Exception:
        log.debug("Incoming link message delete cheyyalekapoyanu.", exc_info=True)

    await send_status(
        message.chat.id,
        "🚀 Link receive ayyindi.\n"
        "🗑️ Link message delete chesanu.\n"
        "⚡ Background processing start chestunnanu.",
    )

    asyncio.create_task(process_link(message.chat.id, url, job_id))


async def main() -> None:
    log.info("Pyrogram polling bot start chestunnanu...")
    await app.start()
    me = await app.get_me()
    log.info("Bot started: @%s (%s)", me.username, me.id)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
