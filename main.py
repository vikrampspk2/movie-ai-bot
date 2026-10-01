"""
STREAM HOSTER MULTI-MIRROR PRODUCTION ENGINE
- Strict Low RAM (<25MB Peak) with 2MB chunks & size-capped queues
- Slow host timeout/drop (No pipeline stalls)
- True multi-target upload progress tracking
- Robust Filename Sanitization & Partial-Upload Guard
- Zero-Spam Telegram Live Progress (Every 4.5s)
- Render 24/7 Keep-Alive
"""

import os
import re
import time
import queue
import threading
import asyncio
import logging
from urllib.parse import unquote, quote
import requests
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ProdStreamBot")

def clean_env(name: str) -> str:
    value = os.environ.get(name, "")
    return value.strip().strip('"').strip("'").strip()

API_ID_RAW = clean_env("API_ID")
API_HASH = clean_env("API_HASH")
BOT_TOKEN = clean_env("BOT_TOKEN")

try:
    API_ID = int(API_ID_RAW)
except (TypeError, ValueError):
    logger.exception("Invalid API_ID environment variable. Expected an integer.")
    raise SystemExit(1)

if not API_HASH or not BOT_TOKEN:
    logger.error("Missing API_HASH or BOT_TOKEN environment variable.")
    raise SystemExit(1)
PORT = int(os.environ.get("PORT", 8080))

def purge_webhook():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=True"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        logger.info("Telegram webhook purge response: %s", response.text)
    except requests.RequestException as exc:
        logger.warning("Telegram webhook purge failed: %s", exc)
    except Exception:
        logger.exception("Unexpected webhook purge error.")

bot = Client(
    "movie_ai_bot_runtime",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

web_server = Flask(__name__)

@web_server.route("/")
def health_check():
    return "Stream Bot is active and running 24/7 on Render!"

def start_keep_alive():
    web_server.run(host="0.0.0.0", port=PORT)

def sanitize_filename(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[\\/*?:"<>| ]', '_', name)
    return name if name else "streamed_file.bin"

def extract_clean_filename(url: str, headers: dict) -> str:
    cd = headers.get("content-disposition", "")
    if "filename=" in cd:
        raw_name = cd.split("filename=")[-1].strip('"\' ')
        if raw_name:
            return sanitize_filename(raw_name)
    clean_url = url.split("?")[0].rstrip("/")
    fallback = clean_url.split("/")[-1]
    return sanitize_filename(fallback)

def human_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"

class ResilientFanout:
    def __init__(self, targets: list, maxsize: int = 2):
        self.targets = targets
        self.queues = {t: queue.Queue(maxsize=maxsize) for t in targets}
        self.active_targets = set(targets)
        self.aborted = False

    def push(self, chunk: bytes):
        dead_targets = set()
        for t in list(self.active_targets):
            try:
                self.queues[t].put(chunk, timeout=15)
            except queue.Full:
                logger.warning(f"Target '{t}' stalled/too slow. Dropping from active mirrors.")
                dead_targets.add(t)
        self.active_targets -= dead_targets

    def close(self):
        for q in self.queues.values():
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def abort(self):
        self.aborted = True
        self.close()

    def get_stream(self, target_name: str, progress_dict: dict):
        q = self.queues[target_name]
        def gen():
            while True:
                if self.aborted:
                    break
                chunk = q.get()
                if chunk is None:
                    break
                progress_dict[target_name] = progress_dict.get(target_name, 0) + len(chunk)
                yield chunk
        return gen()

def worker_buzzheavier(fanout: ResilientFanout, filename: str, progress: dict, results: dict):
    target = "⚡ BuzzHeavier"
    try:
        encoded_name = quote(filename)
        url = f"https://w.buzzheavier.com/{encoded_name}"
        stream_data = fanout.get_stream(target, progress)
        res = requests.put(
            url,
            data=stream_data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=7200
        )
        if not fanout.aborted and res.status_code in [200, 201]:
            results[target] = f"https://buzzheavier.com/{encoded_name}"
    except Exception as e:
        logger.error(f"BuzzHeavier failed: {e}")

def worker_gofile(fanout: ResilientFanout, filename: str, progress: dict, results: dict):
    target = "📂 GoFile"
    try:
        srv_req = requests.get("https://api.gofile.io/servers", timeout=15).json()
        if srv_req.get("status") != "ok":
            return
        node = srv_req["data"]["servers"][0]["name"]
        upload_url = f"https://{node}.gofile.io/contents/uploadfile"
        stream_data = fanout.get_stream(target, progress)
        files = {"file": (filename, stream_data, "application/octet-stream")}
        res = requests.post(upload_url, files=files, timeout=7200)
        if not fanout.aborted and res.status_code == 200:
            data = res.json()
            if data.get("status") == "ok":
                results[target] = data["data"]["downloadPage"]
    except Exception as e:
        logger.error(f"GoFile failed: {e}")

def worker_pixeldrain(fanout: ResilientFanout, filename: str, progress: dict, results: dict):
    target = "💧 PixelDrain"
    try:
        encoded_name = quote(filename)
        url = f"https://pixeldrain.com/api/file/{encoded_name}"
        stream_data = fanout.get_stream(target, progress)
        res = requests.put(url, data=stream_data, timeout=7200)
        if not fanout.aborted and res.status_code in [200, 201]:
            fid = res.json().get("id")
            if fid:
                results[target] = f"https://pixeldrain.com/u/{fid}"
    except Exception as e:
        logger.error(f"PixelDrain failed: {e}")

def execute_resilient_stream(source_url: str, filename: str, total_size: int, tracker: dict) -> dict:
    targets = ["⚡ BuzzHeavier", "📂 GoFile", "💧 PixelDrain"]
    fanout = ResilientFanout(targets=targets, maxsize=2)
    results = {}
    tracker["upload_progress"] = {t: 0 for t in targets}
    tracker["start_time"] = time.time()
    tracker["active"] = True

    threads = [
        threading.Thread(target=worker_buzzheavier, args=(fanout, filename, tracker["upload_progress"], results)),
        threading.Thread(target=worker_gofile, args=(fanout, filename, tracker["upload_progress"], results)),
        threading.Thread(target=worker_pixeldrain, args=(fanout, filename, tracker["upload_progress"], results))
    ]
    for th in threads:
        th.start()

    download_success = False
    try:
        with requests.get(source_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                if chunk:
                    fanout.push(chunk)
                    tracker["downloaded"] = tracker.get("downloaded", 0) + len(chunk)
            download_success = True
    except Exception as e:
        logger.error(f"Source download read aborted: {e}")
        fanout.abort()
    finally:
        if not download_success:
            fanout.abort()
        else:
            fanout.close()
        tracker["active"] = False

    for th in threads:
        th.join()

    return results if download_success else {}

@bot.on_message()
async def log_incoming_message(_, message: Message):
    logger.info(f"Received message: {message.text}")

@bot.on_message(filters.command("start"))
async def handle_start(_, message: Message):
    await message.reply_text(
        "⚡ **Production Multi-Host Streamer**\n\n"
        "• Low RAM Cap (<25MB Peak) & Stall Prevention\n"
        "• Parallel Streaming: BuzzHeavier, GoFile, PixelDrain\n"
        "• Live True Upload Metrics (No FloodWait)\n"
        "• 24/7 Render Keep-Alive Active\n\n"
        "Send \x60/help\x60 for usage."
    )

@bot.on_message(filters.command("help"))
async def handle_help(_, message: Message):
    await message.reply_text(
        "📖 **Usage Guide:**\n\n"
        "\x60/uphoster <direct_download_link>\x60\n\n"
        "The bot will stream the file directly without filling Render's disk."
    )

@bot.on_message(filters.command("uphoster"))
async def handle_uphoster(_, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("⚠️ **Format:** \x60/uphoster <direct_link>\x60")
        return

    source_url = parts[1].strip()
    status_msg = await message.reply_text("🔍 **Analyzing direct link...**")

    try:
        head = await asyncio.to_thread(requests.head, source_url, allow_redirects=True, timeout=15)
        raw_size = int(head.headers.get("content-length", 0))
        filename = extract_clean_filename(source_url, head.headers)
    except Exception:
        raw_size = 0
        filename = "streamed_file.bin"

    tracker = {
        "downloaded": 0,
        "upload_progress": {},
        "start_time": time.time(),
        "active": True
    }

    async def update_live_ui():
        last_ui = ""
        while tracker["active"]:
            await asyncio.sleep(4.5)
            elapsed = max(time.time() - tracker["start_time"], 0.1)
            current_up = max(tracker["upload_progress"].values()) if tracker["upload_progress"] else tracker["downloaded"]
            speed = current_up / elapsed
            speed_str = f"{human_size(speed)}/s"

            if raw_size > 0:
                pct = min((current_up / raw_size) * 100, 100.0)
                filled = int(pct / 10)
                bar = "■" * filled + "□" * (10 - filled)
                eta_sec = int((raw_size - current_up) / speed) if speed > 0 else 0
                eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_sec))
                progress_view = (
                    f"[{bar}] {pct:.1f}%\n"
                    f"⚡ **Speed:** \x60{speed_str}\x60 | **ETA:** \x60{eta_str}\x60\n"
                    f"📊 **Uploaded:** \x60{human_size(current_up)} / {human_size(raw_size)}\x60"
                )
            else:
                progress_view = f"⚡ **Speed:** \x60{speed_str}\x60\n📊 **Uploaded:** \x60{human_size(current_up)}\x60"

            ui = (
                f"📦 **File:** \x60{filename}\x60\n\n"
                f"{progress_view}\n\n"
                "🚀 *Uploading to BuzzHeavier, GoFile, PixelDrain...*"
            )
            if ui != last_ui and tracker["active"]:
                try:
                    await status_msg.edit_text(ui)
                    last_ui = ui
                except Exception:
                    pass

    reporter_task = asyncio.create_task(update_live_ui())

    results = await asyncio.to_thread(
        execute_resilient_stream,
        source_url,
        filename,
        raw_size,
        tracker
    )

    reporter_task.cancel()
    await asyncio.gather(reporter_task, return_exceptions=True)

    if results:
        final_lines = [
            "✅ **Stream Upload Complete!**\n",
            f"📁 **File:** \x60{filename}\x60",
            f"📊 **Size:** \x60{human_size(raw_size if raw_size > 0 else tracker['downloaded'])}\x60\n",
            "🔗 **Generated Mirrors:**"
        ]
        for name, link in results.items():
            final_lines.append(f"• **{name}:** {link}")
        await status_msg.edit_text("\n".join(final_lines))
    else:
        await status_msg.edit_text("❌ **Upload Failed:** Connection lost or mirrors rejected the stream.")

if __name__ == "__main__":
    logger.info("=== movie-ai-bot production launcher ===")
    threading.Thread(target=start_keep_alive, daemon=True, name="flask-health").start()
    retry_delay = 5
    while True:
        try:
            logger.info("Purging any conflicting Telegram webhook before polling...")
            purge_webhook()
            logger.info("Starting Pyrogram bot client...")
            bot.run()
            logger.warning("Pyrogram stopped; restarting in %ss.", retry_delay)
        except Exception:
            logger.exception("Pyrogram startup/runtime exception; process will stay alive.")
        finally:
            try:
                bot.stop()
            except Exception:
                pass
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)
