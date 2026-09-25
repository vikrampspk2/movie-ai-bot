import os
import time
import asyncio
from typing import Dict, Any
import httpx
from fastapi import FastAPI, Request, Response
import uvicorn

OWNER_ID = 8742037337
TELEGRAM_API = "https://api.telegram.org/bot"

message_route: Dict[int, Dict[str, Any]] = {}
settings = {
    "protect_content": True,
    "auto_delete_24h": True
}
delete_queue: list = []

def get_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""

async def call_tg(method: str, payload: dict) -> dict:
    token = get_token()
    if not token:
        return {}
    url = f"{TELEGRAM_API}{token}/{method}"
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            res = await client.post(url, json=payload)
            return res.json()
        except Exception:
            return {}

def cleanup_routing():
    now = time.time()
    expired = [m for m, d in message_route.items() if now - d.get("created_at", 0) > 172800]
    for m in expired:
        message_route.pop(m, None)

async def process_delete_queue():
    now = time.time()
    for item in list(delete_queue):
        c_id, m_id, t_time = item
        if now >= t_time:
            await call_tg("deleteMessage", {"chat_id": c_id, "message_id": m_id})
            if item in delete_queue:
                delete_queue.remove(item)

app = FastAPI()

@app.on_event("startup")
async def start_periodic_cleanup():
    async def loop():
        while True:
            await asyncio.sleep(900)
            cleanup_routing()
            await process_delete_queue()
    asyncio.create_task(loop())

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    cleanup_routing()
    if "message" not in data:
        return Response(status_code=200)

    msg = data["message"]
    chat_id = msg.get("chat", {}).get("id")
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    text = msg.get("text", "")
    reply_to = msg.get("reply_to_message")
    protect = settings["protect_content"]

    if user_id == OWNER_ID:
        if text.strip() == "/help":
            help_text = (
                "👑 *Owner Control Panel*\n\n"
                "• /forward on /forward off - Forward protection\n"
                "• /24h on /24h off - 24-hour auto delete\n"
                "• /to <user_id> <message> - Send direct message to user\n"
                "• Direct reply to relayed message"
            )
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": help_text, "parse_mode": "Markdown"})
            return Response(status_code=200)

        if text.strip() == "/forward off":
            settings["protect_content"] = True
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "✅ Forward protection ON."})
            return Response(status_code=200)
        elif text.strip() == "/forward on":
            settings["protect_content"] = False
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "⚠️ Forward protection OFF."})
            return Response(status_code=200)

        if text.strip() == "/24h on":
            settings["auto_delete_24h"] = True
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "✅ 24h auto-delete ON."})
            return Response(status_code=200)
        elif text.strip() == "/24h off":
            settings["auto_delete_24h"] = False
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "⚠️ 24h auto-delete OFF."})
            return Response(status_code=200)

        if text.startswith("/to "):
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                res = await call_tg("sendMessage", {"chat_id": parts[1], "text": parts[2], "protect_content": protect})
                if res.get("ok") and settings["auto_delete_24h"]:
                    delete_queue.append((int(parts[1]), res["result"]["message_id"], time.time() + 86400))
            return Response(status_code=200)

        if reply_to:
            r_id = reply_to.get("message_id")
            route = message_route.get(r_id)
            if route:
                target_user = route["user_id"]
                res = await call_tg("copyMessage", {
                    "chat_id": target_user,
                    "from_chat_id": OWNER_ID,
                    "message_id": msg["message_id"],
                    "protect_content": protect
                })
                if res.get("ok") and settings["auto_delete_24h"]:
                    delete_queue.append((target_user, res["result"]["message_id"], time.time() + 86400))
                    delete_queue.append((OWNER_ID, msg["message_id"], time.time() + 86400))
            return Response(status_code=200)

    else:
        if text.startswith("/forward") or text.startswith("/24h") or text.startswith("/to"):
            return Response(status_code=200)

        if text.strip() == "/help":
            help_msg = (
                "🤖 *Bot ni ela vadali:*\n\n"
                "• Normal ga message pampandi.\n"
                "• Photo, video, file, voice, audio, sticker kuda pampochu.\n"
                "• Mee message owner ki mee peru + User ID tho private ga relay avuthundi.\n"
                "• Owner reply chesthe aa reply meeku direct ga vastundi.\n\n"
                "🔐 *Security:*\n"
                "• Bot local ga media/file save cheyyadu.\n"
                "• Forward protection default ga ON.\n"
                "• Auto delete default ga 24 hours."
            )
            await call_tg("sendMessage", {"chat_id": chat_id, "text": help_msg, "parse_mode": "Markdown"})
            return Response(status_code=200)

        full_name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
        username = f"@{from_user.get('username')}" if from_user.get('username') else "None"
        header = f"📩 *New private message*\n\n👤 *Name:* {full_name}\n🆔 *User ID:* {user_id}\n🔗 {username}"
        await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": header, "parse_mode": "Markdown"})

        relayed = await call_tg("copyMessage", {
            "chat_id": OWNER_ID,
            "from_chat_id": chat_id,
            "message_id": msg["message_id"],
            "protect_content": protect
        })

        if relayed.get("ok"):
            r_msg_id = relayed["result"]["message_id"]
            message_route[r_msg_id] = {"user_id": user_id, "created_at": time.time()}
            if settings["auto_delete_24h"]:
                delete_queue.append((OWNER_ID, r_msg_id, time.time() + 86400))
                delete_queue.append((chat_id, msg["message_id"], time.time() + 86400))

    return Response(status_code=200)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
