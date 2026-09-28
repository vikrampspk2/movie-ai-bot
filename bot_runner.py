import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pyrogram import Client, filters
from pyrogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vikky-bot")

# ============================================================
# CREDENTIALS SETUP
# ============================================================
API_ID = int(
    os.getenv("PYROGRAM_API_ID")
    or os.getenv("API_ID")
    or os.getenv("TELEGRAM_API_ID")
    or os.getenv("BOT_API_ID")
    or "0"
)

API_HASH = (
    os.getenv("PYROGRAM_API_HASH")
    or os.getenv("API_HASH")
    or os.getenv("TELEGRAM_API_HASH")
    or os.getenv("BOT_API_HASH")
)

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TG_BOT_TOKEN")
)

# ============================================================
# RENDER DUMMY HTTP SERVER (PORT 10000)
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Render health server listening on port {port}")
    return server

# ============================================================
# PYROGRAM CLIENT
# ============================================================
app = Client(
    "vikky_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# ============================================================
# UI BUTTONS
# ============================================================
MAIN_BUTTONS = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("📦 Fast Encode", callback_data="btn_encode"),
            InlineKeyboardButton("🎬 4K Upscale", callback_data="btn_upscale"),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="btn_status"),
            InlineKeyboardButton("📖 Help", callback_data="btn_help"),
        ],
    ]
)

# ============================================================
# MESSAGE LOGGER & HANDLERS (PUBLIC ACCESS)
# ============================================================
@app.on_message(filters.all, group=-100)
async def message_logger(client, message):
    user_id = message.from_user.id if message.from_user else 0
    text = message.text or message.caption or "[Media/Non-text]"
    print(f"MSG: {user_id} -> {text}", flush=True)

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    welcome_text = "Namaskaram! Bot active ga undi. Fast Encode / 4K Upscale ready."
    await message.reply_text(welcome_text, reply_markup=MAIN_BUTTONS)

@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    help_text = "Bot Usage Guide: /start, /help, /status, /encode, /upscale. Send video link to process."
    await message.reply_text(help_text, reply_markup=MAIN_BUTTONS)

@app.on_message(filters.command("status"))
async def status_cmd(client, message):
    status_text = "Status: Bot is Active & Online. Queue ready."
    await message.reply_text(status_text, reply_markup=MAIN_BUTTONS)

@app.on_message(filters.command("encode"))
async def encode_cmd(client, message):
    encode_text = "Fast Video Encode Ready. Send video file or link."
    await message.reply_text(encode_text, reply_markup=MAIN_BUTTONS)

@app.on_message(filters.command("upscale"))
async def upscale_cmd(client, message):
    upscale_text = "4K AI Upscale Ready. Send video file or link."
    await message.reply_text(upscale_text, reply_markup=MAIN_BUTTONS)

# ============================================================
# CALLBACK QUERY HANDLER
# ============================================================
@app.on_callback_query()
async def callbacks(client, callback_query):
    data = callback_query.data
    await callback_query.answer()

    if data == "btn_encode":
        await callback_query.message.reply_text("📦 Fast Encode kosam video file leda link pampandi.")
    elif data == "btn_upscale":
        await callback_query.message.reply_text("🎬 4K Upscale kosam video file leda link pampandi.")
    elif data == "btn_status":
        await callback_query.message.reply_text("📊 Bot server active ga undi. Tasks kosam ready!")
    elif data == "btn_help":
        await callback_query.message.reply_text("📖 Video link pampagane processing options kanipisthayi.")

# ============================================================
# MAIN ENTRYPOINT
# ============================================================
async def main():
    health_server = start_health_server()
    try:
        log.info("Starting Pyrogram client...")
        await app.start()

        # Webhook clear chesi polling ki updates vachelaga chustundi
        try:
            await app.delete_webhook(drop_pending_updates=True)
            log.info("Telegram webhook clear ayyindi.")
        except Exception as e:
            log.warning(f"Webhook delete chesetappudu error: {e}")

        # Telegram menu commands force update
        try:
            commands = [
                BotCommand("start", "Bot ni start cheyandi"),
                BotCommand("help", "Bot usage guidance"),
                BotCommand("status", "Current status check"),
                BotCommand("encode", "Fast video encoding"),
                BotCommand("upscale", "4K video upscaling"),
            ]
            await app.set_bot_commands(commands, scope=BotCommandScopeDefault())
            log.info("Telegram default command menu force update ayyindi.")
        except Exception as e:
            log.warning(f"Commands set chesetappudu error: {e}")

        me = await app.get_me()
        log.info(f"Bot prarambham ayyindi: @{me.username} ({me.id})")
        print("PYROGRAM POLLING IS LIVE", flush=True)

        await asyncio.Event().wait()
    finally:
        log.info("Stopping bot...")
        try:
            await app.stop()
        except Exception:
            pass
        try:
            health_server.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
