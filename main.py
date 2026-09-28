import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Health server to keep Render Live
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_health, daemon=True).start()

# Pyrogram Setup
API_ID = int(os.getenv("API_ID") or os.getenv("TELEGRAM_API_ID") or 0)
API_HASH = os.getenv("API_HASH") or os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    await message.reply_text("Namaskaram! Bot active ga undi.")

async def main():
    await app.start()
    await app.delete_webhook(drop_pending_updates=True)
    print("PYROGRAM LIVE", flush=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
