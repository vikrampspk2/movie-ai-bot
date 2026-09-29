import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GH_PAT_TOKEN = os.environ.get("GH_PAT_TOKEN")
GH_REPO = os.environ.get("GH_REPO")

# Clear old webhooks
requests.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteWebhook?drop_pending_updates=true")

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Vikky 4K Controller Online 24/7")

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    server.serve_forever()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Vikky 4K AI Upscaler Online lo undi bro!\nVideo link send cheyandi leda /upscale <link> ivvandi.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Usage:\nDirect video URL or Google Drive link send cheyandi.")

async def handle_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.startswith("/upscale"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            url = parts[1].strip()
        else:
            await update.message.reply_text("Bro link miss ayyindi! Example: /upscale <link>")
            return
    else:
        url = text

    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("Request register ayyindi bro. GitHub Actions trigger chesthunna...")

    headers = {
        "Authorization": f"Bearer {GH_PAT_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "event_type": "start_upscale",
        "client_payload": {"url": url, "chat_id": chat_id}
    }
    res = requests.post(f"https://api.github.com/repos/{GH_REPO}/dispatches", headers=headers, json=payload)

    if res.status_code == 204:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text="✅ GitHub Actions start ayyindi bro! Background lo AI upscale avthundi. Prathi 2 mins ki live status update vasthundi."
        )
    else:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"❌ GitHub trigger fail ayyindi!\nStatus Code: {res.status_code}\nResponse: {res.text}\nCheck: GH_REPO and GH_PAT_TOKEN permissions."
        )

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("upscale", handle_process))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_process))
    app.run_polling()
