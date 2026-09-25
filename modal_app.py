from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import modal
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

APP_NAME = "vikky-movie-ai-bot"
OWNER_ID = 8742037337
DELETE_AFTER = 24 * 60 * 60

app = modal.App(APP_NAME)
telegram_secret = modal.Secret.from_name("vikky-telegram", required_keys=["TELEGRAM_BOT_TOKEN"])

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("fastapi[standard]>=0.115,<1", "httpx>=0.28,<1")
)

web_app = FastAPI()
forward_enabled = False
auto_delete_enabled = True

# RAM only. No media/files/database are written by this relay.
routes: dict[int, tuple[int, float]] = {}
expiry: dict[tuple[int, int], float] = {}


def tg(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    with httpx.Client(timeout=35) as client:
        response = client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data["result"]


def protect() -> bool:
    return not forward_enabled


def send_text(chat_id: int, text: str, *, protect_content: bool | None = None) -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if protect_content is not None:
        payload["protect_content"] = protect_content
    return tg("sendMessage", payload)


def copy_message(to_chat: int, from_chat: int, message_id: int) -> dict:
    return tg(
        "copyMessage",
        {
            "chat_id": to_chat,
            "from_chat_id": from_chat,
            "message_id": message_id,
            "protect_content": protect(),
        },
    )


def delete_message(chat_id: int, message_id: int) -> None:
    try:
        tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


def remember_message(chat_id: int, message_id: int) -> None:
    if auto_delete_enabled:
        expiry[(chat_id, message_id)] = time.time() + DELETE_AFTER


def user_label(user: dict[str, Any]) -> str:
    name = " ".join(
        x for x in [user.get("first_name"), user.get("last_name")] if x
    ).strip() or "Unknown"
    username = f"@{user['username']}" if user.get("username") else "no username"
    return f"👤 {name}\n🆔 {user.get('id')}\n🔗 {username}"


def help_user() -> str:
    return (
        "🤖 Bot ni ela vadalo:\n\n"
        "• Normal ga text/message pampandi.\n"
        "• Photo, video, file, voice, audio, sticker, animation kuda pampochu.\n"
        "• Mee message owner ki mee peru + User ID tho private ga relay avuthundi.\n"
        "• Owner reply chesthe adi malli meeku direct ga vastundi.\n\n"
        "🔐 Security:\n"
        "• Bot local ga media/file save cheyyadu.\n"
        "• Forward protection default ga ON.\n"
        "• Auto delete default ga 24 hours.\n\n"
        "Help kosam /help."
    )


def owner_help() -> str:
    return (
        "🔐 Owner controls:\n\n"
        "/to USER_ID MESSAGE  → aa user ki text pampu\n"
        "/forward on|off      → forward/save protection ON/OFF\n"
        "/24h on|off           → relay messages 24h auto-delete ON/OFF\n"
        "/help                 → ee help\n\n"
        "User message meeda reply chesthe direct ga aa user ki velthundi.\n"
        "Media caption lo /to USER_ID pedithe direct aa user ki media velthundi."
    )


def parse_to(text: str) -> tuple[int, str] | None:
    parts = text.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/to":
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    return user_id, parts[2] if len(parts) > 2 else ""


async def expiry_loop() -> None:
    while True:
        now = time.time()
        expired = [key for key, deadline in expiry.items() if deadline <= now]
        for chat_id, message_id in expired:
            delete_message(chat_id, message_id)
            expiry.pop((chat_id, message_id), None)

        stale = [
            message_id
            for message_id, (_, created) in routes.items()
            if now - created > DELETE_AFTER
        ]
        for message_id in stale:
            routes.pop(message_id, None)

        await asyncio.sleep(30)


@web_app.on_event("startup")
async def startup() -> None:
    # Only /help is shown in the Telegram command menu.
    tg(
        "setMyCommands",
        {
            "commands": [
                {
                    "command": "help",
                    "description": "Bot ela vadalo Telugu/Tenglish help",
                }
            ]
        },
    )
    asyncio.create_task(expiry_loop())


@web_app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "telegram-relay",
        "forward_protection": not forward_enabled,
        "auto_delete_24h": auto_delete_enabled,
        "storage": "ram-only",
    }


@web_app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    message = update.get("message") or update.get("edited_message")
    if not message:
        return JSONResponse({"ok": True})

    chat = message.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    message_id = int(message.get("message_id") or 0)
    if not chat_id or not message_id:
        return JSONResponse({"ok": True})

    user = message.get("from") or {}
    text = str(message.get("text") or message.get("caption") or "").strip()

    # OWNER SIDE
    if chat_id == OWNER_ID:
        if text.lower() == "/help":
            sent = send_text(OWNER_ID, owner_help(), protect_content=True)
            remember_message(OWNER_ID, int(sent["message_id"]))
            return JSONResponse({"ok": True})

        if text.lower().startswith("/forward "):
            value = text.split(maxsplit=1)[1].strip().lower()
            if value in {"on", "off"}:
                global forward_enabled
                forward_enabled = value == "on"
                state = "OFF (forward allowed)" if forward_enabled else "ON (forward/save blocked)"
                sent = send_text(
                    OWNER_ID,
                    f"Forward protection: {state}",
                    protect_content=True,
                )
                remember_message(OWNER_ID, int(sent["message_id"]))
            return JSONResponse({"ok": True})

        if text.lower().startswith("/24h "):
            value = text.split(maxsplit=1)[1].strip().lower()
            if value in {"on", "off"}:
                global auto_delete_enabled
                auto_delete_enabled = value == "on"
                if not auto_delete_enabled:
                    expiry.clear()
                sent = send_text(
                    OWNER_ID,
                    "24h auto delete: " + ("ON" if auto_delete_enabled else "OFF"),
                    protect_content=True,
                )
                remember_message(OWNER_ID, int(sent["message_id"]))
            return JSONResponse({"ok": True})

        parsed = parse_to(text)
        if parsed:
            target_id, body = parsed
            if body:
                sent = send_text(target_id, body, protect_content=protect())
                remember_message(target_id, int(sent["message_id"]))
            elif any(
                message.get(key)
                for key in ("photo", "video", "document", "audio", "voice", "animation", "sticker")
            ):
                copied = copy_message(target_id, OWNER_ID, message_id)
                remember_message(target_id, int(copied["message_id"]))
            else:
                sent = send_text(
                    OWNER_ID,
                    "Format: /to USER_ID MESSAGE",
                    protect_content=True,
                )
                remember_message(OWNER_ID, int(sent["message_id"]))
            remember_message(OWNER_ID, message_id)
            return JSONResponse({"ok": True})

        reply = message.get("reply_to_message") or {}
        route = routes.get(int(reply.get("message_id") or 0))
        if route:
            target_id, _ = route
            copied = copy_message(target_id, OWNER_ID, message_id)
            remember_message(target_id, int(copied["message_id"]))
            remember_message(OWNER_ID, message_id)
            return JSONResponse({"ok": True})

        # Owner messages without /to or Reply are not broadcast.
        return JSONResponse({"ok": True})

    # USER SIDE
    if text.lower() == "/help":
        sent = send_text(chat_id, help_user(), protect_content=True)
        remember_message(chat_id, int(sent["message_id"]))
        remember_message(chat_id, message_id)
        return JSONResponse({"ok": True})

    header = send_text(
        OWNER_ID,
        "📩 New private message\n"
        + user_label(user)
        + "\n\n↩️ Reply to this header or the copied message to answer.",
        protect_content=True,
    )
    header_id = int(header["message_id"])
    routes[header_id] = (chat_id, time.time())
    remember_message(OWNER_ID, header_id)

    try:
        copied = copy_message(OWNER_ID, chat_id, message_id)
        copied_id = int(copied["message_id"])
        routes[copied_id] = (chat_id, time.time())
        remember_message(OWNER_ID, copied_id)
    except Exception as exc:
        error = send_text(
            OWNER_ID,
            f"⚠️ Could not relay message from {chat_id}: {exc}",
            protect_content=True,
        )
        remember_message(OWNER_ID, int(error["message_id"]))

    # No local file/database copy is made.
    remember_message(chat_id, message_id)
    return JSONResponse({"ok": True})


@app.function(
    image=image,
    secrets=[telegram_secret],
    min_containers=1,
    max_containers=1,
    scaledown_window=300,
    timeout=86400,
)
@modal.asgi_app()
def api():
    return web_app
