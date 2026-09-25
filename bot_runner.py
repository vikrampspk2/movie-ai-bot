import os
import sys
import time
import httpx

OWNER_ID = 8742037337
TELEGRAM_API = "https://api.telegram.org/bot"
message_route = {}
settings = {"protect_content": True, "auto_delete_24h": True}
delete_queue = []
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""

if not TOKEN:
    print("FATAL: No BOT_TOKEN found in Secrets!")
    sys.exit(1)

def call_tg(method, payload):
    try:
        with httpx.Client(timeout=30.0) as client:
            return client.post(f"{TELEGRAM_API}{TOKEN}/{method}", json=payload).json()
    except Exception as e:
        print(f"Error calling {method}: {e}")
        return {}

def cleanup():
    now = time.time()
    for m, d in list(message_route.items()):
        if now - d.get("created_at", 0) > 172800:
            message_route.pop(m, None)
    for item in list(delete_queue):
        c_id, m_id, t_time = item
        if now >= t_time:
            call_tg("deleteMessage", {"chat_id": c_id, "message_id": m_id})
            if item in delete_queue:
                delete_queue.remove(item)

print("Deleting webhook for polling...")
call_tg("deleteWebhook", {"drop_pending_updates": False})
call_tg("setMyCommands", {"commands": [{"command": "help", "description": "Ela vadalo telusukondi"}]})
print("Bot is LIVE and listening for messages...")
offset = 0

while True:
    try:
        cleanup()
        res = call_tg("getUpdates", {"offset": offset, "timeout": 20})
        if not res.get("ok"):
            time.sleep(2)
            continue
        for update in res.get("result", []):
            offset = update["update_id"] + 1
            if "message" not in update:
                continue
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            from_user = msg.get("from", {})
            user_id = from_user.get("id")
            text = msg.get("text", "")
            reply_to = msg.get("reply_to_message")
            protect = settings["protect_content"]

            if user_id == OWNER_ID:
                if text.strip() == "/help":
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "Owner Control Panel\n\n/forward on / /forward off - Forward protection\n/24h on / /24h off - 24-hour auto delete\n/to <user_id> <message> - Send direct message to user\nDirect reply to relayed message", "parse_mode": "Markdown"})
                    continue
                if text.strip() == "/forward off":
                    settings["protect_content"] = True
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "Forward protection ON."})
                    continue
                if text.strip() == "/forward on":
                    settings["protect_content"] = False
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "Forward protection OFF."})
                    continue
                if text.strip() == "/24h on":
                    settings["auto_delete_24h"] = True
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "24h auto-delete ON."})
                    continue
                if text.strip() == "/24h off":
                    settings["auto_delete_24h"] = False
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "24h auto-delete OFF."})
                    continue
                if text.startswith("/to "):
                    parts = text.split(maxsplit=2)
                    if len(parts) >= 3:
                        sent = call_tg("sendMessage", {"chat_id": parts[1], "text": parts[2], "protect_content": protect})
                        if sent.get("ok") and settings["auto_delete_24h"]:
                            delete_queue.append((int(parts[1]), sent["result"]["message_id"], time.time() + 86400))
                    continue
                if reply_to:
                    route = message_route.get(reply_to.get("message_id"))
                    if route:
                        sent = call_tg("copyMessage", {"chat_id": route["user_id"], "from_chat_id": OWNER_ID, "message_id": msg["message_id"], "protect_content": protect})
                        if sent.get("ok") and settings["auto_delete_24h"]:
                            delete_queue.append((route["user_id"], sent["result"]["message_id"], time.time() + 86400))
                            delete_queue.append((OWNER_ID, msg["message_id"], time.time() + 86400))
                    continue
            else:
                if text.startswith("/forward") or text.startswith("/24h") or text.startswith("/to"):
                    continue
                if text.strip() == "/help":
                    call_tg("sendMessage", {"chat_id": chat_id, "text": "Bot ni ela vadali:\n\nNormal ga message pampandi.\nPhoto, video, file, voice, audio, sticker kuda pampochu.\nMee message owner ki mee peru + User ID tho private ga relay avuthundi.\nOwner reply chesthe aa reply meeku direct ga vastundi.\n\nSecurity:\nBot local ga media/file save cheyyadu.\nForward protection default ga ON.\nAuto delete default ga 24 hours.", "parse_mode": "Markdown"})
                    continue
                full_name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
                username = f"@{from_user.get('username')}" if from_user.get('username') else "None"
                header = f"New private message\n\nName: {full_name}\nUser ID: {user_id}\n{username}"
                call_tg("sendMessage", {"chat_id": OWNER_ID, "text": header, "parse_mode": "Markdown"})
                relayed = call_tg("copyMessage", {"chat_id": OWNER_ID, "from_chat_id": chat_id, "message_id": msg["message_id"], "protect_content": protect})
                if relayed.get("ok"):
                    r_msg_id = relayed["result"]["message_id"]
                    message_route[r_msg_id] = {"user_id": user_id, "created_at": time.time()}
                    if settings["auto_delete_24h"]:
                        delete_queue.append((OWNER_ID, r_msg_id, time.time() + 86400))
                        delete_queue.append((chat_id, msg["message_id"], time.time() + 86400))
    except Exception as err:
        print(f"Loop iteration error: {err}")
        time.sleep(2)
