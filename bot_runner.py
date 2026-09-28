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

from pyrogram import Client, StopPropagation, filters
from pyrogram import raw
from pyrogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import settings
from app.encode import EncodeError, encode_to_mkv
from app.media.downloader import DownloadError, download_url
from app.uploaders import upload_to_all
from app.upscale import UpscaleError, upscale_4k

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vikky-bot")

ENV_ALIASES = {
    "api_id": ("PYROGRAM_API_ID", "API_ID", "TELEGRAM_API_ID", "BOT_API_ID"),
    "api_hash": ("PYROGRAM_API_HASH", "API_HASH", "TELEGRAM_API_HASH", "BOT_API_HASH"),
    "bot_token": ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN", "TG_BOT_TOKEN"),
}
DOTENV_FILES = (Path.cwd() / ".env", Path.cwd() / ".env.production", Path(__file__).resolve().parent / ".env", Path(__file__).resolve().parent.parent / ".env")

def _clean(value: object) -> str:
    return str(value).strip().strip(chr(34)).strip(chr(39)) if value is not None else ""

def _read_dotenv_files() -> dict[str, str]:
    values: dict[str, str] = {}
    wanted = {name for names in ENV_ALIASES.values() for name in names}
    for path in DOTENV_FILES:
        try:
            if not path.is_file():
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in wanted:
                    values.setdefault(key, _clean(value.split(" #", 1)[0].strip()))
        except (OSError, UnicodeError):
            log.debug("Env file read cheyyalekapoyanu: %s", path, exc_info=True)
    return values

def _first_value(names: tuple[str, ...], dotenv_values: dict[str, str]) -> str:
    for name in names:
        value = _clean(os.environ.get(name, ""))
        if value:
            return value
    for name in names:
        value = _clean(dotenv_values.get(name, ""))
        if value:
            return value
    return ""

def _resolve_credentials() -> tuple[int, str, str, tuple[str, ...]]:
    dotenv_values = _read_dotenv_files()
    configured_api_id = _clean(getattr(settings, "telegram_api_id", ""))
    configured_api_hash = _clean(getattr(settings, "telegram_api_hash", ""))
    configured_bot_token = _clean(getattr(settings, "telegram_bot_token", ""))
    api_id_raw = configured_api_id or _first_value(ENV_ALIASES["api_id"], dotenv_values)
    api_hash = configured_api_hash or _first_value(ENV_ALIASES["api_hash"], dotenv_values)
    bot_token = configured_bot_token or _first_value(ENV_ALIASES["bot_token"], dotenv_values)
    try:
        api_id = int(api_id_raw) if api_id_raw else 0
        if api_id < 1:
            api_id = 0
    except (TypeError, ValueError):
        api_id = 0
    missing = []
    if not api_id: missing.append("API_ID")
    if not api_hash: missing.append("API_HASH")
    if not bot_token: missing.append("BOT_TOKEN")
    return api_id, api_hash, bot_token, tuple(missing)

def _resolve_owner_id() -> int:
    names = ("OWNER_ID", "TELEGRAM_OWNER_ID", "ADMIN_ID", "TELEGRAM_ADMIN_ID")
    dotenv_values = _read_dotenv_files()
    raw_value = _first_value(names, dotenv_values)
    try:
        return int(raw_value) if raw_value else 0
    except (TypeError, ValueError):
        return 0


API_ID, API_HASH, BOT_TOKEN, _MISSING_CREDENTIALS = _resolve_credentials()
OWNER_ID = _resolve_owner_id()
app = Client("vikky_bot", api_id=API_ID or 1, api_hash=API_HASH or "waiting-for-credentials", bot_token=BOT_TOKEN or "waiting-for-credentials", in_memory=True)

URL_RE = re.compile(r"https?://[^\s<>\"]+")

# Oka GPU upscale mariyu oka CPU encode okesari nadavachu.
# GPU-GPU mariyu CPU-CPU rendu okesari nadavavu.
GPU_RUNNING = False
CPU_RUNNING = False
job_queue: deque["Job"] = deque()
jobs: dict[str, "Job"] = {}
chat_jobs: dict[int, str] = {}
scheduler_lock = asyncio.Lock()


@dataclass
class Job:
    id: str
    chat_id: int
    url: str
    kind: str | None = None
    status: str = "waiting_choice"
    stage: str = "waiting_choice"
    work_dir: Path | None = None
    source: Path | None = None
    output: Path | None = None
    slot: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    heartbeat_task: asyncio.Task | None = field(default=None, repr=False)
    created_at: float = field(default_factory=monotonic)
    cancelled: bool = False
    control_message_id: int | None = None


def find_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,);]}>\"'") if match else None


def menu_keyboard(job: Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 4K Upscale", callback_data=f"choose:upscale:{job.id}"
                ),
                InlineKeyboardButton(
                    "📦 Fast Encode", callback_data=f"choose:encode:{job.id}"
                ),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{job.id}")],
        ]
    )


def selected_keyboard(job: Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Back", callback_data=f"back:{job.id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{job.id}"),
            ]
        ]
    )


def processing_keyboard(job: Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Cancel Task", callback_data=f"cancel:{job.id}")]
        ]
    )


def queue_keyboard(job: Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Back", callback_data=f"back:{job.id}"),
                InlineKeyboardButton(
                    "🛑 Cancel Task", callback_data=f"cancel:{job.id}"
                ),
            ]
        ]
    )


async def say(chat_id: int, text: str, reply_markup=None) -> Message:
    return await app.send_message(chat_id, text, reply_markup=reply_markup)


async def edit_control(job: Job, text: str, reply_markup=None) -> None:
    if not job.control_message_id:
        return
    try:
        await app.edit_message_text(
            job.chat_id,
            job.control_message_id,
            text,
            reply_markup=reply_markup,
        )
    except Exception:
        log.debug("Control message edit failed", exc_info=True)


def stage_text(job: Job) -> str:
    return {
        "download": "cinema file download avuthondi",
        "encode": "video encode avuthondi",
        "upscale": "4K AI upscale avuthondi",
        "upload": "ready file links siddham avuthunnayi",
    }.get(job.stage, "movie pani nadusthondi")


def progress_bar(job: Job) -> str:
    # Indeterminate live bar: nijamaina percentage lenappudu fake percentage chupinchadu.
    frames = ("▰▱▱▱▱", "▰▰▱▱▱", "▰▰▰▱▱", "▰▰▰▰▱", "▰▰▰▰▰")
    index = int(monotonic() / 2) % len(frames)
    return frames[index]


async def heartbeat(job: Job) -> None:
    try:
        while True:
            await asyncio.sleep(8)
            if job.cancelled or job.status != "running":
                return
            text = (
                f"⏳ {stage_text(job)}\n"
                f"{progress_bar(job)}\n"
                "Pani background lo konasaguthondi."
            )
            await edit_control(job, text, processing_keyboard(job))
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("Heartbeat failed for job %s", job.id)


def cancel_job(job: Job) -> None:
    job.cancelled = True
    if job.task and not job.task.done():
        job.task.cancel()


async def cleanup_job(job: Job) -> None:
    if job.heartbeat_task and not job.heartbeat_task.done():
        job.heartbeat_task.cancel()
    if job.work_dir:
        try:
            await asyncio.to_thread(shutil.rmtree, job.work_dir, True)
        except Exception:
            log.exception("Temporary cleanup failed for job %s", job.id)
    jobs.pop(job.id, None)
    if chat_jobs.get(job.chat_id) == job.id:
        chat_jobs.pop(job.chat_id, None)


def slot_available(kind: str) -> bool:
    if kind == "upscale":
        return not GPU_RUNNING
    if kind == "encode":
        return not CPU_RUNNING
    return False


def reserve_slot(job: Job) -> None:
    global GPU_RUNNING, CPU_RUNNING
    if job.kind == "upscale":
        GPU_RUNNING = True
        job.slot = "gpu"
    elif job.kind == "encode":
        CPU_RUNNING = True
        job.slot = "cpu"


async def release_slot(job: Job) -> None:
    global GPU_RUNNING, CPU_RUNNING
    if job.slot == "gpu":
        GPU_RUNNING = False
    elif job.slot == "cpu":
        CPU_RUNNING = False
    job.slot = None


async def scheduler() -> None:
    # Queue ni FIFO ga chustham, kani oka resource busy unte vere resource
    # available unte daniki saripoye task ni skip chesi start chestham.
    async with scheduler_lock:
        while job_queue:
            started = False
            for job in list(job_queue):
                if job.cancelled or job.kind not in {"upscale", "encode"}:
                    continue
                if not slot_available(job.kind):
                    continue

                try:
                    job_queue.remove(job)
                except ValueError:
                    continue

                reserve_slot(job)
                job.status = "running"
                job.stage = "download"
                job.task = asyncio.create_task(run_job(job))
                started = True

            if not started:
                return


async def enqueue_job(chat_id: int, url: str, kind: str | None = None) -> Job:
    job = Job(
        id=os.urandom(6).hex(),
        chat_id=chat_id,
        url=url,
        kind=kind,
        status="queued" if kind else "waiting_choice",
        stage="queued" if kind else "waiting_choice",
        work_dir=Path(tempfile.mkdtemp(prefix="vikky-")),
    )
    jobs[job.id] = job
    chat_jobs[chat_id] = job.id
    if kind:
        job_queue.append(job)
    return job


async def run_job(job: Job) -> None:
    try:
        await edit_control(
            job,
            "🚀 Pani modalaindi.\n"
            "📥 Cinema file download chestunnanu.",
            processing_keyboard(job),
        )

        job.source = await download_url(job.url, job.work_dir)
        if job.cancelled:
            raise asyncio.CancelledError

        if job.kind == "upscale":
            job.stage = "upscale"
            await edit_control(
                job,
                "🎬 Download poorthayyindi.\n"
                "✨ Ippudu 4K AI upscale chestunnanu.\n"
                f"{progress_bar(job)}",
                processing_keyboard(job),
            )
            job.output = job.work_dir / "Vikky AI Upscale 4K.mkv"
            job.heartbeat_task = asyncio.create_task(heartbeat(job))
            await asyncio.to_thread(
                upscale_4k,
                job.source,
                job.output,
                job.work_dir / "upscale-work",
            )

        elif job.kind == "encode":
            job.stage = "encode"
            await edit_control(
                job,
                "🎬 Download poorthayyindi.\n"
                "⚙️ Ippudu fast video encode chestunnanu.\n"
                f"{progress_bar(job)}",
                processing_keyboard(job),
            )
            job.output = job.work_dir / "Vikky encoding.mkv"
            job.heartbeat_task = asyncio.create_task(heartbeat(job))
            await asyncio.to_thread(
                encode_to_mkv,
                job.source,
                job.output,
                3.0,
                5.0,
            )
        else:
            raise RuntimeError("Pani rakam select kaaledu.")

        if job.cancelled:
            raise asyncio.CancelledError

        job.stage = "upload"
        await edit_control(
            job,
            "✅ Video pani poorthayyindi.\n"
            "☁️ Ippudu output links siddham chestunnanu.\n"
            f"{progress_bar(job)}",
            processing_keyboard(job),
        )
        links = await upload_to_all(job.output)
        if job.cancelled:
            raise asyncio.CancelledError

        good = {
            key: value
            for key, value in links.items()
            if value and not str(value).startswith("ERROR:")
        }
        if not good:
            raise RuntimeError("Upload links raledu.")

        job.status = "completed"
        await edit_control(
            job,
            "🎉 Mee cinema siddham ayyindi!\n\n"
            + "\n".join(f"🔗 {name}: {link}" for name, link in good.items()),
            selected_keyboard(job),
        )
        job.control_message_id = None

    except asyncio.CancelledError:
        job.status = "cancelled"
        try:
            await say(
                job.chat_id,
                "🛑 Mee pani aapabadindi.\n"
                "🧹 Temporary files clean chestunnanu.",
            )
        except Exception:
            log.exception("Cancel message failed for job %s", job.id)
        raise

    except (DownloadError, EncodeError, UpscaleError):
        job.status = "failed"
        log.exception("Known processing failure for job %s", job.id)
        try:
            await say(
                job.chat_id,
                "❌ Cinema pani poorthi kaaledu.\n"
                "Konchem sepu tarvata malli prayatninchandi.",
            )
        except Exception:
            log.exception("Failure message failed for job %s", job.id)

    except Exception:
        job.status = "failed"
        log.exception("Unexpected job failure for %s", job.id)
        try:
            await say(
                job.chat_id,
                "❌ Cinema pani lo anukoni ibbandi vachindi.\n"
                "Konchem sepu tarvata malli prayatninchandi.",
            )
        except Exception:
            log.exception("Unexpected failure message failed for job %s", job.id)

    finally:
        await release_slot(job)
        await cleanup_job(job)
        await scheduler()


async def cancel_queued_job(job: Job, query: CallbackQuery) -> None:
    job.cancelled = True
    try:
        job_queue.remove(job)
    except ValueError:
        pass
    await cleanup_job(job)
    try:
        await query.message.edit_text(
            "❌ Mee pani aapabadindi.\n🧹 Temporary files clean chesanu."
        )
    except Exception:
        log.debug("Queued cancel edit failed", exc_info=True)
    await query.answer("Pani aapabadindi.")


@app.on_message(filters.private, group=-1)
async def access_and_debug_handler(_, message: Message) -> None:
    user = message.from_user
    user_id = user.id if user else 0
    command_text = message.text or message.caption or ""
    log.info("Incoming Telegram message | user_id=%s | text=%s", user_id, command_text[:500])

    if OWNER_ID and user_id != OWNER_ID:
        await message.reply_text(
            f"Access Denied: Mee Telegram ID ({user_id}) authorized kadu. "
            "Render Environment lo OWNER_ID check chesukondi."
        )
        raise StopPropagation


@app.on_callback_query()
async def callback_handler(_, query: CallbackQuery) -> None:
    data = query.data or ""
    parts = data.split(":")
    job = jobs.get(parts[-1]) if len(parts) >= 2 else None

    if not job:
        await query.answer("Ee pani ippudu dorakadam ledu.", show_alert=True)
        return

    if data.startswith("cancel:"):
        if job.status in {"waiting_choice", "queued"}:
            await cancel_queued_job(job, query)
        else:
            cancel_job(job)
            await query.answer("Pani aaputunnanu.")
        return

    if data.startswith("back:"):
        if job.status != "queued":
            await query.answer(
                "Ippudu venakki velladam saadhyam kaadu.", show_alert=True
            )
            return
        job.kind = None
        job.status = "waiting_choice"
        job.stage = "waiting_choice"
        try:
            job_queue.remove(job)
        except ValueError:
            pass
        await query.message.edit_text(
            "🎬 Mee cinema ki em cheyyali?\n"
            "Oka pani enchukondi.",
            reply_markup=menu_keyboard(job),
        )
        await query.answer("Venakki vacham.")
        await scheduler()
        return

    if data.startswith("choose:"):
        if job.status != "waiting_choice":
            await query.answer(
                "Ee pani ippatike process lo undi.", show_alert=True
            )
            return

        kind = parts[1] if len(parts) > 1 else ""
        if kind not in {"upscale", "encode"}:
            await query.answer("Ee option sari kaadu.", show_alert=True)
            return

        job.kind = kind
        job.status = "queued"
        job.stage = "queued"
        job_queue.append(job)

        title = (
            "🎬 4K AI upscale"
            if kind == "upscale"
            else "📦 Fast video encode"
        )
        await query.message.edit_text(
            f"🎯 {title} enchukunnaru.\n"
            "⏳ Queue mariyu pani slot chustunnanu.",
            reply_markup=queue_keyboard(job),
        )
        await query.answer("Pani enchukunnaru.")
        await scheduler()
        return

    await query.answer()


@app.on_message(filters.private & filters.command("start"))
async def start_handler(_, message: Message) -> None:
    await message.reply_text(
        "🙏 Namaskaram!\n\n"
        "🎬 Ee bot cinema links ni automatic ga download, encode, 4K AI upscale "
        "mariyu upload chestundi.\n\n"
        "🔗 Link pampiste ventane link message delete chesi, meeku pani "
        "enchukune buttons chupistundi.\n\n"
        "/help - Bot ni ela vadalo\n"
        "/status - Mee pani mariyu queue sthithi\n"
        "/encode - Fast video encode\n"
        "/upscale - 4K video upscale"
    )


@app.on_message(filters.private & filters.command("help"))
async def help_handler(_, message: Message) -> None:
    await message.reply_text(
        "📖 Bot vadatam ila:\n\n"
        "1️⃣ Direct cinema link pampandi.\n"
        "2️⃣ Link message ventane delete avuthundi.\n"
        "3️⃣ 🎬 4K Upscale leda 📦 Fast Encode enchukondi.\n"
        "4️⃣ Pani busy unte automatic ga queue lo vechi untundi.\n"
        "5️⃣ Oka GPU upscale mariyu oka CPU encode okesari nadavagalavu.\n"
        "6️⃣ Pani nadusthunnappudu 🛑 Cancel Task tho aapavachu.\n"
        "7️⃣ Aapina pani ki temporary files clean chestham.\n"
        "8️⃣ Pani poorthi ayyaka output links vastayi.\n\n"
        "⚡ /encode <link> - Fast encode direct ga modalu pettadaniki\n"
        "✨ /upscale <link> - 4K AI upscale direct ga modalu pettadaniki\n"
        "📊 /status - Mee current pani mariyu queue sthithi chudataniki"
    )


@app.on_message(filters.private & filters.command("status"))
async def status_handler(_, message: Message) -> None:
    job_id = chat_jobs.get(message.chat.id)
    job = jobs.get(job_id) if job_id else None

    running_gpu = sum(1 for item in jobs.values() if item.status == "running" and item.slot == "gpu")
    running_cpu = sum(1 for item in jobs.values() if item.status == "running" and item.slot == "cpu")
    waiting = sum(1 for item in job_queue if item.status == "queued")

    if not job:
        await message.reply_text(
            "📊 Mee peru meeda ippudu pani emi ledu.\n\n"
            f"🔥 Nadusthunnavi: GPU {running_gpu}, CPU {running_cpu}\n"
            f"⏳ Queue lo: {waiting}"
        )
        return

    if job.status == "waiting_choice":
        detail = "Mee cinema ki pani enchukovali."
    elif job.status == "queued":
        try:
            position = list(job_queue).index(job) + 1
        except ValueError:
            position = 0
        detail = f"Queue lo mee sthaanam: {position}"
    else:
        detail = stage_text(job)

    await message.reply_text(
        "📊 Mee pani sthithi\n\n"
        f"🆔 Gurtimpu: {job.id}\n"
        f"📌 Sthithi: {detail}\n\n"
        f"🔥 GPU: {running_gpu} pani\n"
        f"🧠 CPU: {running_cpu} pani\n"
        f"⏳ Queue: {waiting} pani"
    )


async def direct_command(message: Message, kind: str) -> None:
    url = find_url(" ".join(message.command[1:]) if message.command else "")
    if not url:
        await message.reply_text(
            f"/{kind} <cinema link> ila pampandi."
        )
        return

    if message.chat.id in chat_jobs:
        await message.reply_text(
            "Mee mundu pani inka nadusthondi.\n"
            "/status tho sthithi chudandi."
        )
        return

    try:
        await message.delete()
    except Exception:
        log.debug("Command message delete cheyyalekapoyanu.", exc_info=True)

    job = await enqueue_job(message.chat.id, url, kind)
    msg = await say(
        message.chat.id,
        (
            "🎯 Mee pani teesukunnanu.\n"
            "⏳ Queue mariyu pani slot chustunnanu."
        ),
        queue_keyboard(job),
    )
    job.control_message_id = msg.id
    await scheduler()


@app.on_message(filters.private & filters.command("encode"))
async def encode_command(_, message: Message) -> None:
    await direct_command(message, "encode")


@app.on_message(filters.private & filters.command("upscale"))
async def upscale_command(_, message: Message) -> None:
    await direct_command(message, "upscale")


@app.on_message(
    filters.private
    & filters.text
    & ~filters.command(["start", "help", "status", "encode", "upscale"])
)
async def link_handler(_, message: Message) -> None:
    url = find_url(message.text or "")
    if not url:
        await message.reply_text(
            "🎬 Direct cinema link pampandi.\n"
            "/help tho poorthi margadarshakam chudandi."
        )
        return

    if message.chat.id in chat_jobs:
        await message.reply_text(
            "Mee mundu pani inka nadusthondi.\n"
            "/status tho sthithi chudandi."
        )
        return

    try:
        await message.delete()
    except Exception:
        log.debug("Link message delete cheyyalekapoyanu.", exc_info=True)

    # Ikkada inka queue cheyyamu. Mundu user pani enchukovali.
    job = await enqueue_job(message.chat.id, url)
    msg = await say(
        message.chat.id,
        "🔗 Link teesukunnanu.\n"
        "🎬 Ippudu mee pani enchukondi.",
        menu_keyboard(job),
    )
    job.control_message_id = msg.id


async def refresh_bot_commands() -> None:
    # Webhook ni polling mundu force ga clear chestham.
    try:
        await app.invoke(raw.functions.bots.DeleteWebhook(drop_pending_updates=True))
        log.info("Telegram webhook clear ayyindi; pending updates drop ayyayi.")
    except Exception:
        log.exception("Telegram webhook clear cheyyadam lo ibbandi.")
        raise

    scope = BotCommandScopeDefault()
    try:
        await app.delete_bot_commands(scope=scope)
    except Exception:
        log.exception("Pata default Telegram commands clear cheyyadam lo ibbandi.")
        raise

    commands = [
        BotCommand("start", "Bot prarambha vivaralu"),
        BotCommand("help", "Poorthi vaduka margadarshakam"),
        BotCommand("status", "Pani mariyu queue sthithi"),
        BotCommand("encode", "Tvarita video encode"),
        BotCommand("upscale", "4K video AI upscale"),
    ]
    await app.set_bot_commands(commands, scope=scope)
    log.info("Telegram default command menu force update ayyindi.")


async def main() -> None:
    global API_ID, API_HASH, BOT_TOKEN, app
    log.info("Pyrogram polling bot prarambhistunnanu...")
    last_missing: tuple[str, ...] | None = None
    reported_missing_at = 0.0
    retry_delay = 30
    while True:
        try:
            API_ID, API_HASH, BOT_TOKEN, missing = _resolve_credentials()
            if missing:
                now = monotonic()
                if missing != last_missing or now - reported_missing_at >= 600:
                    log.warning("Telegram credentials inka dorakaledu: %s. Render/.env values check cheyyandi.", ", ".join(missing))
                    last_missing = missing
                    reported_missing_at = now
                await asyncio.sleep(retry_delay)
                continue
            app = Client("vikky_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
            await app.start()
            await refresh_bot_commands()
            me = await app.get_me()
            log.info("Bot prarambham ayyindi: @%s (%s)", me.username, me.id)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            log.info("Bot pani aapabadindi.")
            raise
        except Exception:
            log.exception("Bot runtime lo ibbandi vachindi; clean restart chestanu.")
            try:
                if app.is_connected:
                    await app.stop()
            except Exception:
                log.debug("Bot stop cleanup lo ibbandi.", exc_info=True)
            await asyncio.sleep(retry_delay)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot shutdown.")
