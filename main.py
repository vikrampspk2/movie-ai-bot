import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pyrogram import Client

class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthServer)
    server.serve_forever()

threading.Thread(target=start_server, daemon=True).start()

API_ID = int(os.getenv("API_ID") or os.getenv("TELEGRAM_API_ID") or 0)
API_HASH = os.getenv("API_HASH") or os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

app = Client("clean_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

@app.on_message()
def reply_all(client, message):
    print(f"Message received: {message.text}", flush=True)
    message.reply_text("Namaskaram! Bot active ga undi.")

if __name__ == "__main__":
    print("Starting bot...", flush=True)
    app.run()
