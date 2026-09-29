import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GH_PAT_TOKEN = os.environ.get("GH_PAT_TOKEN")
GH_REPO = os.environ.get("GH_REPO")

# 1. Patha webhook delete chesthunnam (Conflicts clear avvadaniki)
requests.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteWebhook?drop_pending_updates=true")

# 2. Render 24/7 online unchadaniki dummy web server
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Vikky 4K Bot Server Active 24/7")

def start_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    server.serve_forever()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Vikky 4K AI Upscaler Bot Online lo undi bro!\nDirect ga video link pampinchina leda /upscale <link> ichina process start avthundi.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Usage:\n1. Direct link, Google Drive link, leda streaming URL pampandi.\n2. Automatic ga zero-loss 4K upscale aipoyi download link vasthundi.")

async def process_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text("Link vachindi bro! Background lo zero-loss 4K upscale start ayyindi. File ready ayyaka direct link pamputhanu...")

    # GitHub Actions trigger chesthunnam
    headers = {
        "Authorization": f"Bearer {GH_PAT_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "event_type": "start_upscale",
        "client_payload": {"url": url, "chat_id": chat_id}
    }
    requests.post(f"https://api.github.com/repos/{GH_REPO}/dispatches", headers=headers, json=payload)

if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("upscale", process_trigger))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), process_trigger))
    app.run_polling()
