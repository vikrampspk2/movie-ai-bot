from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any

from pyrogram import Client, filters
from pyrogram.types import Message

try:
    from kaggle.api.kaggle_api_extended import KaggleApi
except Exception:
    KaggleApi = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vikky-bot")

API_ID = int(os.getenv("PYROGRAM_API_ID") or os.getenv("API_ID") or "0")
API_HASH = os.getenv("PYROGRAM_API_HASH") or os.getenv("API_HASH") or ""
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
OWNER_ID = int(os.getenv("OWNER_ID", "8742037337"))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Missing Telegram credentials: PYROGRAM_API_ID, "
        "PYROGRAM_API_HASH and TELEGRAM_BOT_TOKEN are required."
    )

app = Client(
    "vikky_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

message_route: dict[int, int] = {}
delete_queue: deque[tuple[int, int, float]] = deque()

settings = {
    "protect_content": True,
    "auto_delete_24h": True,
}

_kaggle_api: Any = None


def get_kaggle_api() -> Any:
    global _kaggle_api
    if _kaggle_api is not None:
        return _kaggle_api
    if KaggleApi is None:
        return None
    try:
        api = KaggleApi()
        api.authenticate()
        _kaggle_api = api
        log.info("Kaggle API authenticated")
        return api
    except Exception as exc:
        log.warning("Kaggle API unavailable: %s", exc)
        return None


async def kaggle_status() -> str:
    api = await asyncio.to_thread(get_kaggle_api)
    if api is None:
        return "Kaggle API is not authenticated/configured."
    try:
        user = await asyncio.to_thread(api.get_current_user)
        username = getattr(user, "username", None) or getattr(user, "name", None) or "authenticated"
        return f"Kaggle API connected: {username}"
    except Exception as exc:
        log.warning("Kaggle status failed: %s", exc)
        return "Kaggle credentials are present, but the API check failed."


def schedule_delete(chat_id: int, message_id: int) -> None:
    if settings["auto_delete_24h"]:
        delete_queue.append((chat_id, message_id, time.time() + 86400))


async def cleanup_loop() -> None:
    while True:
        try:
            now = time.time()
            while delete_queue and delete_queue[0][2] <= now:
                chat_id, message_id, _ = delete_queue.popleft()
                try:
                    await app.delete_messages(chat_id, message_id)
                except Exception as exc:
                    log.debug("Delete failed for %s/%s: %s", chat_id, message_id, exc)
        except Exception:
            log.exception("Cleanup loop error")
        await asyncio.sleep(30)


@app.on_message(filters.private & filters.command(["start", "help"]))
async def help_handler(_, message: Message) -> None:
    if message.from_user and message.from_user.id == OWNER_ID:
        await message.reply_text(
            "Owner Control Panel\n\n"
            f"Forward protection: {'ON' if settings['protect_content'] else 'OFF'}\n"
            f"24h auto-delete: {'ON' if settings['auto_delete_24h'] else 'OFF'}\n\n"
            "/forward_on\n"
            "/forward_off\n"
            "/delete24h_on\n"
            "/delete24h_off\n"
            "/to <user_id> <message>\n"
            "/kaggle\n\n"
            "Reply to a relayed user message to answer that user."
        )
    else:
        await message.reply_text(
            "Welcome! Send any message, photo, video, audio or document. "
            "It will be privately delivered to the admin."
        )


@app.on_message(filters.private & filters.user(OWNER_ID) & filters.command("forward_on"))
async def forward_on(_, message: Message) -> None:
    settings["protect_content"] = True
    await message.reply_text("Forward protection is ON.")


@app.on_message(filters.private & filters.user(OWNER_ID) & filters.command("forward_off"))
async def forward_off(_, message: Message) -> None:
    settings["protect_content"] = False
    await message.reply_text("Forward protection is OFF.")


@app.on_message(filters.private & filters.user(OWNER_ID) & filters.command("delete24h_on"))
async def delete_on(_, message: Message) -> None:
    settings["auto_delete_24h"] = True
    await message.reply_text("24-hour auto-delete is ON.")


@app.on_message(filters.private & filters.user(OWNER_ID) & filters.command("delete24h_off"))
async def delete_off(_, message: Message) -> None:
    settings["auto_delete_24h"] = False
    await message.reply_text("24-hour auto-delete is OFF.")


@app.on_message(filters.private & filters.user(OWNER_ID) & filters.command("kaggle"))
async def kaggle_command(_, message: Message) -> None:
    await message.reply_text(await kaggle_status())


@app.on_message(filters.private & filters.user(OWNER_ID) & filters.command("to"))
async def send_to_user(_, message: Message) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply_text("Usage: /to <user_id> <message>")
        return

    try:
        target = int(parts[1])
    except ValueError:
        await message.reply_text("Invalid Telegram user ID.")
        return

    sent = await app.send_message(
        target,
        parts[2],
        protect_content=settings["protect_content"],
    )
    schedule_delete(target, sent.id)
    await message.reply_text(f"Delivered to {target}.")


@app.on_message(filters.private & filters.user(OWNER_ID) & ~filters.service)
async def owner_message(_, message: Message) -> None:
    if message.text and message.text.startswith("/"):
        return

    if message.reply_to_message:
        target = message_route.get(message.reply_to_message.id)
        if target:
            sent = await message.copy(
                target,
                protect_content=settings["protect_content"],
            )
            schedule_delete(target, sent.id)
            schedule_delete(OWNER_ID, message.id)
            return

        text = (message.text or "").strip()
        if text.isdigit() and len(text) >= 6:
            target = int(text)
            sent = await app.copy_message(
                target,
                OWNER_ID,
                message.reply_to_message.id,
                protect_content=settings["protect_content"],
            )
            schedule_delete(target, sent.id)
            schedule_delete(OWNER_ID, message.reply_to_message.id)
            await message.reply_text(f"Sent to {target}.")
            return


@app.on_message(filters.private & ~filters.user(OWNER_ID))
async def relay_to_owner(_, message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if message.text and message.text.startswith("/"):
        return

    full_name = " ".join(
        x for x in [user.first_name, user.last_name] if x
    ).strip() or "Unknown"
    username = f"@{user.username}" if user.username else "No username"

    try:
        relayed = await message.copy(
            OWNER_ID,
            protect_content=settings["protect_content"],
        )
        message_route[relayed.id] = user.id

        info = await app.send_message(
            OWNER_ID,
            f"From: {full_name} | User ID: {user.id} | {username}\n"
            "Reply to this message or the copied message to answer.",
            reply_to_message_id=relayed.id,
        )
        message_route[info.id] = user.id

        schedule_delete(OWNER_ID, relayed.id)
        schedule_delete(OWNER_ID, info.id)
        schedule_delete(message.chat.id, message.id)
    except Exception:
        log.exception("Failed to relay message %s from %s", message.id, user.id)
        await message.reply_text("Message delivery failed. Please try again.")


async def main() -> None:
    log.info("Starting Pyrogram polling bot...")
    await app.start()
    me = await app.get_me()
    log.info("Bot started: @%s (%s)", me.username, me.id)
    asyncio.create_task(cleanup_loop())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
