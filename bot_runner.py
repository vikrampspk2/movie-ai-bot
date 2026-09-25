import os
import sys
import time
import asyncio
import httpx

OWNER_ID = 8742037337
TELEGRAM_API = "https://api.telegram.org/bot"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
if not TOKEN:
    print("FATAL: No BOT_TOKEN found in Secrets!")
    sys.exit(1)

# In-memory routing and settings
# message_route: { owner_received_msg_id: target_user_id }
message_route = {}
# media_route: { owner_sent_msg_id: timestamp }
owner_recent_media = {}

settings = {
    "protect_content": True,
    "auto_delete_24h": True
}
delete_queue = []
processed_updates = set()

client = httpx.AsyncClient(timeout=25.0)

async def call_tg(method: str, payload: dict) -> dict:
    url = f"{TELEGRAM_API}{TOKEN}/{method}"
    try:
        res = await client.post(url, json=payload)
        return res.json()
    except Exception as e:
        print(f"Error calling {method}: {e}")
        return {}

async def register_commands():
    # Set owner commands in Telegram Menu
    owner_scope = {"type": "chat", "chat_id": OWNER_ID}
    owner_cmds = [
        {"command": "help", "description": "👑 Control Panel & Status"},
        {"command": "forward_on", "description": "🔒 Protect Media/Text (No Forward)"},
        {"command": "forward_off", "description": "🔓 Allow Forwarding/Saving"},
        {"command": "delete24h_on", "description": "⏳ Enable 24h Auto-Delete"},
        {"command": "delete24h_off", "description": "♾️ Disable Auto-Delete"}
    ]
    await call_tg("setMyCommands", {"commands": owner_cmds, "scope": owner_scope})

    # Set default user commands
    default_cmds = [
        {"command": "help", "description": "Help & Instructions"}
    ]
    await call_tg("setMyCommands", {"commands": default_cmds})

def cleanup():
    now = time.time()
    # Expire old updates set (prevent memory leak)
    if len(processed_updates) > 5000:
        processed_updates.clear()
    
    # Process 24h deletion queue
    for item in list(delete_queue):
        c_id, m_id, expire_at = item
        if now >= expire_at:
            asyncio.create_task(call_tg("deleteMessage", {"chat_id": c_id, "message_id": m_id}))
            if item in delete_queue:
                delete_queue.remove(item)

async def handle_message(msg: dict):
    msg_id = msg.get("message_id")
    chat_id = msg.get("chat", {}).get("id")
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    reply_to = msg.get("reply_to_message")
    protect = settings["protect_content"]

    is_owner = (str(user_id) == str(OWNER_ID))

    # ===================================================
    # 1. OWNER CONTROLS & SENDING
    # ===================================================
    if is_owner:
        cmd = text.lower()

        # Help / Status
        if cmd in ["/help", "/start"]:
            help_text = (
                "👑 *Owner Control Panel*\n\n"
                f"🔒 *Forward Protection:* `{'ON' if settings['protect_content'] else 'OFF'}`\n"
                f"⏳ *24h Auto-Delete:* `{'ON' if settings['auto_delete_24h'] else 'OFF'}`\n\n"
                "⚙️ *Menu Commands:*\n"
                "• `/forward_on` - Stop users from forwarding/saving\n"
                "• `/forward_off` - Allow users to forward/save\n"
                "• `/delete24h_on` - Enable 24h message deletion\n"
                "• `/delete24h_off` - Disable 24h message deletion\n\n"
                "📤 *How to Message Users:*\n"
                "1. **Direct Reply:** Relayed മെസേജ് కి టెలిగ్రామ్ లో డైరెక్ట్ గా Reply ఇవ్వండి.\n"
                "2. **Media + User ID:** మీరు ఏదైనా ఫోటో/వీడియో/ఫైల్ పంపి, ఆ మెసేజ్ కి Reply గా కేవలం వాడి User ID టైప్ చేసి సెండ్ చేయండి.\n"
                "3. **Text Command:** `/to <user_id> <message>`"
            )
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": help_text, "parse_mode": "Markdown"})
            return

        # Settings: Forward Protection
        if cmd in ["/forward_on", "/forward on"]:
            settings["protect_content"] = True
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "✅ *Forward Protection is ON* (Users cannot save/forward).", "parse_mode": "Markdown"})
            return
        elif cmd in ["/forward_off", "/forward off"]:
            settings["protect_content"] = False
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "⚠️ *Forward Protection is OFF* (Users can save/forward).", "parse_mode": "Markdown"})
            return

        # Settings: 24h Auto Delete
        if cmd in ["/delete24h_on", "/24h on"]:
            settings["auto_delete_24h"] = True
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "✅ *24-Hour Auto-Delete is ON*.", "parse_mode": "Markdown"})
            return
        elif cmd in ["/delete24h_off", "/24h off"]:
            settings["auto_delete_24h"] = False
            await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "⚠️ *24-Hour Auto-Delete is OFF*.", "parse_mode": "Markdown"})
            return

        # Manual /to command
        if text.startswith("/to "):
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                target = parts[1].strip()
                body = parts[2]
                res = await call_tg("sendMessage", {"chat_id": target, "text": body, "protect_content": protect})
                if res.get("ok"):
                    await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"✅ Delivered to `{target}`", "parse_mode": "Markdown"})
                    if settings["auto_delete_24h"]:
                        delete_queue.append((int(target), res["result"]["message_id"], time.time() + 86400))
                else:
                    await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"❌ Delivery failed to `{target}`. Check if user started bot."})
            return

        # Check Reply Cases
        if reply_to:
            target_msg_id = reply_to.get("message_id")

            # Case A: Owner replies with only User ID to a previously uploaded file/photo/text
            clean_digits = "".join(filter(str.isdigit, text))
            if text.isdigit() and len(clean_digits) >= 6:
                target_user = int(clean_digits)
                res = await call_tg("copyMessage", {
                    "chat_id": target_user,
                    "from_chat_id": OWNER_ID,
                    "message_id": target_msg_id,
                    "protect_content": protect
                })
                if res.get("ok"):
                    await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"✅ Sent directly to `{target_user}`!", "parse_mode": "Markdown"})
                    if settings["auto_delete_24h"]:
                        delete_queue.append((target_user, res["result"]["message_id"], time.time() + 86400))
                        delete_queue.append((OWNER_ID, target_msg_id, time.time() + 86400))
                else:
                    await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": f"❌ Failed to forward to `{target_user}`."})
                return

            # Case B: Owner directly replies to an incoming user message
            if target_msg_id in message_route:
                target_user = message_route[target_msg_id]
                res = await call_tg("copyMessage", {
                    "chat_id": target_user,
                    "from_chat_id": OWNER_ID,
                    "message_id": msg_id,
                    "protect_content": protect
                })
                if res.get("ok"):
                    if settings["auto_delete_24h"]:
                        delete_queue.append((target_user, res["result"]["message_id"], time.time() + 86400))
                        delete_queue.append((OWNER_ID, msg_id, time.time() + 86400))
                else:
                    await call_tg("sendMessage", {"chat_id": OWNER_ID, "text": "❌ Reply sending failed."})
                return

    # ===================================================
    # 2. NORMAL USERS -> RELAY TO OWNER (Single Clean Message)
    # ===================================================
    else:
        if text in ["/start", "/help"]:
            await call_tg("sendMessage", {
                "chat_id": chat_id,
                "text": "👋 Welcome! Send any message, photo, video, or file. It will be privately delivered to the admin."
            })
            return

        # Prepare clear header
        full_name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
        username = f"@{from_user.get('username')}" if from_user.get('username') else "No username"

        # Copy message directly to owner
        relayed = await call_tg("copyMessage", {
            "chat_id": OWNER_ID,
            "from_chat_id": chat_id,
            "message_id": msg_id,
            "protect_content": protect
        })

        if relayed.get("ok"):
            r_msg_id = relayed["result"]["message_id"]
            message_route[r_msg_id] = user_id

            # Send single notification to owner so they can reply directly
            info_text = f"📩 *From:* {full_name} | `{user_id}` | {username}\n_(Reply to this or the message above to answer)_"
            info_res = await call_tg("sendMessage", {
                "chat_id": OWNER_ID,
                "text": info_text,
                "parse_mode": "Markdown",
                "reply_to_message_id": r_msg_id
            })
            if info_res.get("ok"):
                info_id = info_res["result"]["message_id"]
                message_route[info_id] = user_id

            if settings["auto_delete_24h"]:
                delete_queue.append((OWNER_ID, r_msg_id, time.time() + 86400))
                delete_queue.append((chat_id, msg_id, time.time() + 86400))

async def main():
    print("Deleting old webhooks...")
    await call_tg("deleteWebhook", {"drop_pending_updates": True})
    await register_commands()
    print("Bot loop started with pure async polling...")

    offset = 0
    while True:
        try:
            cleanup()
            res = await call_tg("getUpdates", {"offset": offset, "timeout": 15})
            if not res.get("ok"):
                await asyncio.sleep(1)
                continue

            updates = res.get("result", [])
            for update in updates:
                up_id = update["update_id"]
                offset = up_id + 1

                if up_id in processed_updates:
                    continue
                processed_updates.add(up_id)

                if "message" in update:
                    asyncio.create_task(handle_message(update["message"]))

        except Exception as e:
            print(f"Error in main polling loop: {e}")
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
