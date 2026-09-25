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
image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "fastapi[standard]>=0.115,<1",
    "httpx>=0.28,<1",
)

settings_store = modal.Dict.from_name("relay-settings", create_if_missing=True)
route_store = modal.Dict.from_name("relay-routes", create_if_missing=True)
delete_queue_store = modal.Dict.from_name("relay-delete-queue", create_if_missing=True)

web_app = FastAPI()


def get_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


async def call_tg(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = get_token()
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data


def is_protected() -> bool:
    return bool(settings_store.get("protect_content", True))


def is_auto_delete_enabled() -> bool:
    return bool(settings_store.get("auto_delete_24h", True))


def queue_delete(chat_id: int, message_id: int) -> None:
    if is_auto_delete_enabled():
        delete_queue_store[f"{chat_id}:{message_id}"] = time.time() + DELETE_AFTER


def queue_route(message_id: int, user_id: int) -> None:
    route_store[str(message_id)] = {
        "user_id": user_id,
        "created_at": time.time(),
    }


async def send_text(chat_id: int, text: str, protect: bool = True) -> dict[str, Any]:
    return (await call_tg(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "protect_content": protect,
        },
    ))["result"]


async def copy_message(to_chat: int, from_chat: int, message_id: int) -> dict[str, Any]:
    return (await call_tg(
        "copyMessage",
        {
            "chat_id": to_chat,
            "from_chat_id": from_chat,
            "message_id": message_id,
            "protect_content": is_protected(),
        },
    ))["result"]


def parse_to(text: str) -> tuple[int, str] | None:
    parts = text.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/to":
        return None
    try:
        target_id = int(parts[1])
    except ValueError:
        return None
    return target_id, parts[2] if len(parts) > 2 else ""


def user_label(user: dict[str, Any]) -> str:
    name = " ".join(
        x for x in (user.get("first_name"), user.get("last_name")) if x
    ).strip() or "Unknown"
    username = f"@{user['username']}" if user.get("username") else "no username"
    return f"👤 {name}\n🆔 {user.get('id')}\n🔗 {username}"


def owner_help() -> str:
    return (
        "🔐 Owner controls:\n\n"
        "/to USER_ID MESSAGE → send text/media to a user\n"
        "/forward on|off → protection ON/OFF\n"
        "/24h on|off → 24-hour auto-delete ON/OFF\n"
        "/help → show this help\n\n"
        "Reply to a relayed header/message to answer that user."
    )


def user_help() -> str:
    return (
        "🤖 Bot ni ela vadalo:\n\n"
        "• Text, photo, video, file, voice, audio, sticker, animation pampochu.\n"
        "• Mee message owner ki private ga relay avuthundi.\n"
        "• Owner reply chesthe direct ga meeku vastundi.\n\n"
        "🔐 Forward protection default ga ON.\n"
        "🗑️ Auto-delete default ga 24 hours."
    )


async def cleanup_expired() -> None:
    now = time.time()

    for key, deadline in list(delete_queue_store.items()):
        try:
            if float(deadline) > now:
                continue
            chat_id, message_id = key.split(":", 1)
            try:
                await call_tg(
                    "deleteMessage",
                    {"chat_id": int(chat_id), "message_id": int(message_id)},
                )
            except Exception:
                pass
            try:
                del delete_queue_store[key]
            except KeyError:
                pass
        except Exception:
            try:
                del delete_queue_store[key]
            except KeyError:
                pass

    for key, value in list(route_store.items()):
        try:
            if now - float(value.get("created_at", 0)) > DELETE_AFTER:
                del route_store[key]
        except Exception:
            try:
                del route_store[key]
            except KeyError:
                pass


@web_app.on_event("startup")
async def startup() -> None:
    await call_tg(
        "setMyCommands",
        {"commands": [{"command": "help", "description": "Bot ela vadalo help"}]},
    )
    asyncio.create_task(cleanup_loop())


async def cleanup_loop() -> None:
    while True:
        try:
            await cleanup_expired()
        except Exception:
            pass
        await asyncio.sleep(30)


@web_app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "telegram-relay",
        "forward_protection": is_protected(),
        "auto_delete_24h": is_auto_delete_enabled(),
        "storage": "persistent metadata only; no media storage",
    }


@web_app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    message = update.get("message")
    if not message:
        return JSONResponse({"ok": True})

    chat_id = int((message.get("chat") or {}).get("id") or 0)
    message_id = int(message.get("message_id") or 0)
    if not chat_id or not message_id:
        return JSONResponse({"ok": True})

    user = message.get("from") or {}
    text = str(message.get("text") or message.get("caption") or "").strip()

    if chat_id == OWNER_ID:
        if text.lower() == "/help":
            sent = await send_text(OWNER_ID, owner_help())
            queue_delete(OWNER_ID, int(sent["message_id"]))
            return JSONResponse({"ok": True})

        if text.lower().startswith("/forward "):
            value = text.split(maxsplit=1)[1].strip().lower()
            if value in {"on", "off"}:
                settings_store["protect_content"] = value == "off"
                sent = await send_text(
                    OWNER_ID,
                    "Forward protection: " + (
                        "OFF (forward/save allowed)"
                        if value == "on"
                        else "ON (forward/save blocked)"
                    ),
                )
                queue_delete(OWNER_ID, int(sent["message_id"]))
            return JSONResponse({"ok": True})

        if text.lower().startswith("/24h "):
            value = text.split(maxsplit=1)[1].strip().lower()
            if value in {"on", "off"}:
                settings_store["auto_delete_24h"] = value == "on"
                if value == "off":
                    for key in list(delete_queue_store.keys()):
                        try:
                            del delete_queue_store[key]
                        except KeyError:
                            pass
                sent = await send_text(
                    OWNER_ID,
                    f"24h auto delete: {'ON' if value == 'on' else 'OFF'}",
                )
                if value == "on":
                    queue_delete(OWNER_ID, int(sent["message_id"]))
            return JSONResponse({"ok": True})

        parsed = parse_to(text)
        if parsed:
            target_id, body = parsed
            try:
                if body:
                    sent = await send_text(target_id, body, is_protected())
                elif any(message.get(k) for k in (
                    "photo", "video", "document", "audio",
                    "voice", "animation", "sticker",
                )):
                    sent = await copy_message(target_id, OWNER_ID, message_id)
                else:
                    sent = None
                    error = await send_text(OWNER_ID, "Format: /to USER_ID MESSAGE")
                    queue_delete(OWNER_ID, int(error["message_id"]))
                if sent:
                    queue_delete(target_id, int(sent["message_id"]))
                queue_delete(OWNER_ID, message_id)
            except Exception as exc:
                error = await send_text(OWNER_ID, f"⚠️ Send failed: {exc}")
                queue_delete(OWNER_ID, int(error["message_id"]))
            return JSONResponse({"ok": True})

        reply = message.get("reply_to_message") or {}
        route = route_store.get(str(int(reply.get("message_id") or 0)))
        if route:
            target_id = int(route["user_id"])
            try:
                copied = await copy_message(target_id, OWNER_ID, message_id)
                queue_delete(target_id, int(copied["message_id"]))
                queue_delete(OWNER_ID, message_id)
            except Exception as exc:
                error = await send_text(OWNER_ID, f"⚠️ Relay reply failed: {exc}")
                queue_delete(OWNER_ID, int(error["message_id"]))
        return JSONResponse({"ok": True})

    if text.lower() == "/help":
        sent = await send_text(chat_id, user_help())
        queue_delete(chat_id, int(sent["message_id"]))
        queue_delete(chat_id, message_id)
        return JSONResponse({"ok": True})

    if text.lower().startswith(("/forward", "/24h", "/to")):
        queue_delete(chat_id, message_id)
        return JSONResponse({"ok": True})

    header = await send_text(
        OWNER_ID,
        "📩 New private message\n\n" + user_label(user)
        + "\n\n↩️ Reply to this header or the copied message to answer.",
    )
    queue_route(int(header["message_id"]), chat_id)
    queue_delete(OWNER_ID, int(header["message_id"]))

    try:
        copied = await copy_message(OWNER_ID, chat_id, message_id)
        queue_route(int(copied["message_id"]), chat_id)
        queue_delete(OWNER_ID, int(copied["message_id"]))
    except Exception as exc:
        error = await send_text(
            OWNER_ID,
            f"⚠️ Could not relay message from {chat_id}: {exc}",
        )
        queue_delete(OWNER_ID, int(error["message_id"]))

    queue_delete(chat_id, message_id)
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
