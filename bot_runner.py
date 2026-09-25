import os
import sys
import time
import httpx

OWNER_ID = 8742037337
TELEGRAM_API = "https://api.telegram.org/bot"

# Routing memory: { relayed_bot_msg_id: {"user_id": int, "created_at": float} }
message_route = {}
settings = {
    "protect_content": True,
    "auto_delete_24h": True
}
delete_queue = []

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""

if not TOKEN:
    print("FATAL: No BOT_TOKEN found in Secrets!")
    sys.exit(1)

def call_tg(method: str, payload: dict) -> dict:
    url = f"{TELEGRAM_API}{TOKEN}/{method}"
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(url, json=payload)
            return res.json()
    except Exception as e:
        print(f"Error calling {method}: {e}")
        return {}

def cleanup():
    now = time.time()
    # Expire old routes
    expired = [m for m, d in message_route.items() if now - d.get("created_at", 0) > 172800]
    for m in expired:
        message_route.pop(m, None)
    
    # Process 24h delete queue
    for item in list(delete_queue):
        c_id, m_id, t_time = item
        if now >= t_time:
            call_tg("deleteMessage", {"chat_id": c_id, "message_id": m_id})
            if item in delete_queue:
                delete_queue.remove(item)

# First delete any existing webhook so polling works
print("Deleting webhook for polling...")
call_tg("deleteWebhook", {"drop_pending_updates": False})

print("Bot is LIVE and listening for messages...")
offset = 0

while True:
    try:
        cleanup()
        res = call_tg("getUpdates", {"offset": offset, "timeout": 20})
        if not res.get("ok"):
            time.sleep(2)
            continue

        updates = res.get("result", [])
        for update in updates:
            offset = update["update_id"] + 1
            if "message" not in update:
                continue

            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            from_user = msg.get("from", {})
            user_id = from_user.get("id")
            text = (msg.get("text") or msg.get("caption") or "").strip()
            reply_to = msg.get("reply_to_message")
            protect = settings["protect_content"]

            # Strict string-based Owner Check
            is_owner = (str(user_id) == str(OWNER_ID))

            # ==========================================
            # 1. OWNER CONTROLS & DISPATCH
            # ==========================================
            if is_owner:
                cmd = text.lower()

                # Owner Help
                if cmd == "/help":
                    help_text = (
                        "👑 *Owner Control Panel*\n\n"
                        "⚙️ *Settings Toggles:*\n"
                        "• `/forward on` / `/forward off` - Forward protection\n"
                        "• `/24h on` / `/24h off` - 24 Hours auto delete\n\n"
                        "📤 *How to Send to Users:*\n"
                        "1. *File/Media to User:* Nuvvu edhaina File/Photo/Video send chesi, daaniki *Reply* ga vadi User ID type chesthe, aa file vadiki vellipothundi.\n"
                        "2. *Normal Reply:* Relayed message ki direct ga Telegram Reply cheyandi.\n"
                        "3. *Command:* `/to <user_id> <message>`\n\n"
                        f"📊 *Current Status:*\n"
                        f"• Forward Protection: `{'ON' if settings['protect_content'] else 'OFF'}`\n"
                        f"• 24h Auto-Delete: `{'ON' if settings['auto_delete_24h'] else 'OFF'}`"
                    )
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": help_text, "parse_mode": "Markdown"})
                    continue

                # Forward Protection Toggle
                if cmd == "/forward on":
                    settings["protect_content"] = True
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "✅ *Forward Protection is now ON* (Users cannot save/forward messages/media).", "parse_mode": "Markdown"})
                    continue
                elif cmd == "/forward off":
                    settings["protect_content"] = False
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "⚠️ *Forward Protection is now OFF* (Users can save/forward messages).", "parse_mode": "Markdown"})
                    continue

                # 24 Hours Auto Delete Toggle
                if cmd == "/24h on":
                    settings["auto_delete_24h"] = True
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "✅ *24-Hour Auto-Delete is now ON* (Messages delete automatically after 24h).", "parse_mode": "Markdown"})
                    continue
                elif cmd == "/24h off":
                    settings["auto_delete_24h"] = False
                    call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "⚠️ *24-Hour Auto-Delete is now OFF*.", "parse_mode": "Markdown"})
                    continue

                # Send via /to command
                if text.startswith("/to "):
                    parts = text.split(maxsplit=2)
                    if len(parts) >= 3:
                        target = parts[1].strip()
                        body = parts[2]
                        res_send = call_tg("sendMessage", {"chat_id": target, "text": body, "protect_content": protect})
                        if res_send.get("ok"):
                            call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"✅ Message sent to `{target}`", "parse_mode": "Markdown"})
                            if settings["auto_delete_24h"]:
                                delete_queue.append((int(target), res_send["result"]["message_id"], time.time() + 86400))
                        else:
                            call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"❌ Failed to send to `{target}`. Check User ID."})
                    continue

                # Reply based dispatch
                if reply_to:
                    # Case A: Owner replies with a User ID to a previously uploaded file/media
                    if text.isdigit() and len(text) >= 6:
                        target_user = int(text)
                        original_msg_id = reply_to.get("message_id")
                        res_copy = call_tg("copyMessage", {
                            "chat_id": target_user,
                            "from_chat_id": OWNER_ID,
                            "message_id": original_msg_id,
                            "protect_content": protect
                        })
                        if res_copy.get("ok"):
                            call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"✅ Media/File successfully forwarded to `{target_user}`!", "parse_mode": "Markdown"})
                            if settings["auto_delete_24h"]:
                                delete_queue.append((target_user, res_copy["result"]["message_id"], time.time() + 86400))
                                delete_queue.append((OWNER_ID, original_msg_id, time.time() + 86400))
                        else:
                            call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"❌ Could not forward media to `{target_user}`. Has the user started the bot?"})
                        continue

                    # Case B: Owner replies to a relayed message from a user
                    r_id = reply_to.get("message_id")
                    route = message_route.get(r_id)
                    if route:
                        target_user = route["user_id"]
                        res_copy = call_tg("copyMessage", {
                            "chat_id": target_user,
                            "from_chat_id": OWNER_ID,
                            "message_id": msg["message_id"],
                            "protect_content": protect
                        })
                        if res_copy.get("ok"):
                            if settings["auto_delete_24h"]:
                                delete_queue.append((target_user, res_copy["result"]["message_id"], time.time() + 86400))
                                delete_queue.append((OWNER_ID, msg["message_id"], time.time() + 86400))
                        else:
                            call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "❌ Reply sending failed."})
                        continue

            # ==========================================
            # 2. NORMAL USERS RELAY TO OWNER
            # ==========================================
            else:
                # Block commands from normal users
                if text.startswith("/"):
                    if text.strip() == "/help" or text.strip() == "/start":
                        help_msg = (
                            "🤖 *Bot Support*\n\n"
                            "Mee message, photo, video, file, voice direct ga relay avuthundi.\n"
                            "Owner reply isthe meeku direct ga ikkade deliver avuthundi."
                        )
                        call_tg("sendMessage", {"chat_id": chat_id, "text": help_msg, "parse_mode": "Markdown"})
                    continue

                full_name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
                username = f"@{from_user.get('username')}" if from_user.get('username') else "None"
                header = f"📩 *New Message*\n\n👤 *From:* {full_name}\n🆔 *User ID:* `{user_id}`\n🔗 *Username:* {username}"
                call_tg("sendMessage", {"chat_id": OWNER_ID, "text": header, "parse_mode": "Markdown"})

                relayed = call_tg("copyMessage", {
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

    except Exception as err:
        print(f"Loop iteration error: {err}")
        time.sleep(2)
