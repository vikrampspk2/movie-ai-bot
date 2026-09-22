from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import modal

APP_NAME = os.getenv("MODAL_APP_NAME", "vikky-movie-ai")
DATA_PATH = "/data"

remote_secret = modal.Secret.from_name(
    "vikky-remote",
    required_keys=["VIKKY_REMOTE_TOKEN"],
)
telegram_secret = modal.Secret.from_name(
    "vikky-telegram",
    required_keys=["TELEGRAM_BOT_TOKEN"],
)
volume = modal.Volume.from_name("vikky-media", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi>=0.115,<1",
        "uvicorn[standard]>=0.34,<1",
        "httpx>=0.28,<1",
    )
)

app = modal.App(APP_NAME)


def _telegram_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _telegram_call(token: str, method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(_telegram_url(token, method), json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"Telegram {method} failed"))
        return data


@app.function(
    image=image,
    secrets=[remote_secret, telegram_secret],
    volumes={DATA_PATH: volume},
    min_containers=1,
    max_containers=2,
    scaledown_window=300,
    timeout=120,
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Header, HTTPException, Request
    from pydantic import BaseModel

    web = FastAPI(title="Vikky Movie AI Control Plane")
    telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
    remote_token = os.environ["VIKKY_REMOTE_TOKEN"]

    class JobRequest(BaseModel):
        job_id: str
        job_type: str
        source_path: str | None = None
        reference_path: str | None = None
        workspace: str | None = None
        checkpoint: str | None = None

    def auth(authorization: str | None) -> None:
        if not remote_token or authorization != f"Bearer {remote_token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    @web.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "vikky-movie-ai",
            "telegram_configured": bool(telegram_token),
            "time": time.time(),
        }

    @web.get("/telegram/setup")
    async def telegram_setup(request: Request):
        base = str(request.base_url).rstrip("/") + "/telegram/webhook"
        await _telegram_call(
            telegram_token,
            "setWebhook",
            {
                "url": base,
                "drop_pending_updates": False,
                "allowed_updates": ["message"],
            },
        )
        me = await _telegram_call(telegram_token, "getMe", {})
        return {
            "ok": True,
            "webhook": base,
            "bot": me.get("result", {}).get("username"),
        }

    @web.get("/telegram/info")
    async def telegram_info():
        return await _telegram_call(telegram_token, "getWebhookInfo", {})

    @web.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ):
        update = await request.json()
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id:
            return {"ok": True}

        if text.startswith("/start"):
            reply = "🎬 Vikky Movie AI Bot is ONLINE.\n\nCommands:\n/status — bot/worker status\n/queue — queue status\n/help — commands"
        elif text.startswith("/help"):
            reply = "🎬 Vikky Movie AI\n/start\n/status\n/queue\n/sync\n/upscale\n/encode"
        elif text.startswith("/status"):
            reply = "🟢 ONLINE\n☁️ Backend: Modal\n📦 Control plane: healthy"
        elif text.startswith("/queue"):
            reply = "📭 Queue is ready. Send a media file to create a job."
        elif text.startswith("/sync"):
            reply = "🔄 SYNC mode selected. Send the media/reference files."
        elif text.startswith("/upscale"):
            reply = "✨ AI UPSCALE 4K mode selected. Send the source media."
        elif text.startswith("/encode"):
            reply = "🎞️ ENCODE mode selected. Send the source media."
        else:
            reply = "✅ Bot is online. Use /help"

        await _telegram_call(
            telegram_token,
            "sendMessage",
            {"chat_id": chat_id, "text": reply},
        )
        return {"ok": True}

    @web.post("/jobs")
    async def submit(job: JobRequest, authorization: str | None = Header(default=None)):
        auth(authorization)
        job_dir = Path(DATA_PATH) / "jobs" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(job.model_dump_json(indent=2), encoding="utf-8")
        volume.commit()
        return {
            "accepted": True,
            "job_id": job.job_id,
            "state": "queued",
            "workspace": str(job_dir),
            "checkpoint": job.checkpoint,
        }

    return web
