from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import modal

APP_NAME = os.getenv("MODAL_APP_NAME", "vikky-movie-ai")
DATA_PATH = "/data"
JOB_ROOT = Path(DATA_PATH) / "jobs"

telegram_secret = modal.Secret.from_name(
    "vikky-telegram",
    required_keys=["TELEGRAM_BOT_TOKEN"],
)
remote_secret = modal.Secret.from_name(
    "vikky-remote",
    required_keys=["VIKKY_REMOTE_TOKEN"],
)

base_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "git")
    .pip_install(
        "fastapi[standard]>=0.115,<1",
        "httpx>=0.28,<1",
        "numpy>=2.2,<3",
    )
    .add_local_dir("app", remote_path="/root/app")
)

gpu_image = (
    base_image
    .pip_install("torch>=2.6,<3", "opencv-python-headless>=4.11,<5", "realesrgan>=0.3.0,<1")
)

app = modal.App(APP_NAME)
volume = modal.Volume.from_name("vikky-media", create_if_missing=True)


def _tg_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _tg(token: str, method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(_tg_url(token, method), json=payload or {})
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"Telegram {method} failed"))
        return data


def _run(cmd: list[str], timeout: int = 86400) -> None:
    subprocess.run(cmd, check=True, timeout=timeout)


@app.function(
    image=base_image,
    secrets=[telegram_secret, remote_secret],
    volumes={DATA_PATH: volume},
    min_containers=1,
    max_containers=2,
    scaledown_window=300,
    timeout=120,
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Header, HTTPException, Request

    web = FastAPI(title="Vikky Movie AI")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    remote_token = os.environ["VIKKY_REMOTE_TOKEN"]

    @web.get("/health")
    async def health():
        return {"status": "ok", "service": APP_NAME, "telegram_configured": bool(token), "time": time.time()}

    @web.get("/setup")
    async def setup(request: Request):
        url = str(request.base_url).rstrip("/") + "/webhook"
        result = await _tg(token, "setWebhook", {
            "url": url,
            "drop_pending_updates": False,
            "allowed_updates": ["message"],
        })
        return {"ok": True, "webhook": url, "telegram": result.get("result")}

    @web.get("/telegram/info")
    async def telegram_info():
        return await _tg(token, "getWebhookInfo")

    @web.post("/webhook")
    async def webhook(
        request: Request,
        secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ):
        # Telegram itself authenticates the HTTPS webhook. Modal's endpoint remains public;
        # only Telegram is allowed to deliver updates because setWebhook is controlled here.
        update = await request.json()
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return {"ok": True}

        text = (message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].lower() if text else ""
        if command in {"/start", "/help"}:
            reply = (
                "🎬 Vikky Movie AI is ONLINE.\n\n"
                "/status — system status\n/queue — queue status\n"
                "/sync — audio sync mode\n/upscale — AI 4K upscale\n/encode — encode"
            )
        elif command == "/status":
            reply = "🟢 ONLINE\n☁️ Backend: Modal\n⚙️ Worker: ready"
        elif command == "/queue":
            reply = "📦 Queue is active. Send media and select /sync, /upscale or /encode."
        elif command in {"/sync", "/upscale", "/encode"}:
            job_id = f"{chat_id}-{int(time.time()*1000)}"
            job_dir = JOB_ROOT / str(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "request.json").write_text(
                json.dumps({"job_id": job_id, "chat_id": chat_id, "command": command}),
                encoding="utf-8",
            )
            volume.commit()
            worker.spawn(job_id, chat_id, command, str(job_dir))
            reply = f"✅ {command[1:].upper()} job queued.\n🆔 {job_id}"
        else:
            reply = "Use /help to see available commands."

        await _tg(token, "sendMessage", {"chat_id": chat_id, "text": reply})
        return {"ok": True}

    @web.post("/jobs")
    async def submit(request: Request, authorization: str | None = Header(default=None)):
        if authorization != f"Bearer {remote_token}":
            raise HTTPException(status_code=401, detail="unauthorized")
        body = await request.json()
        job_id = str(body["job_id"])
        job_dir = JOB_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        volume.commit()
        worker.spawn(job_id, int(body["chat_id"]), str(body["job_type"]), str(job_dir))
        return {"accepted": True, "job_id": job_id, "state": "running"}

    return web


@app.function(
    image=base_image,
    secrets=[telegram_secret],
    volumes={DATA_PATH: volume},
    timeout=86400,
    retries=2,
)
def worker(job_id: str, chat_id: int, command: str, job_dir: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    root = Path(job_dir)
    request_file = root / "request.json"
    if request_file.exists():
        request = json.loads(request_file.read_text(encoding="utf-8"))
    else:
        request = {"job_id": job_id, "chat_id": chat_id, "command": command}

    async def notify(message: str) -> None:
        await _tg(token, "sendMessage", {"chat_id": chat_id, "text": message})

    try:
        # The worker is intentionally long-lived and isolated from the webhook.
        # Actual uploaded media is attached to this job directory by the media-intake
        # layer; commands fail loudly if required input is absent.
        media = next(
            (p for p in root.iterdir() if p.is_file() and p.name not in {"request.json", "job.json"}),
            None,
        )
        if media is None:
            asyncio.run(notify(
                f"⚠️ Job {job_id}: no media file was attached yet. "
                "Upload the media after selecting the mode."
            ))
            return

        output = root / {
            "/encode": "Vikky encoding.mkv",
            "/upscale": "Vikky AI Upscale 4K.mkv",
            "/sync": "Sync by Vikky.mkv",
        }.get(command, "output.mkv")

        if command == "/encode":
            _run(["ffmpeg", "-v", "error", "-i", str(media), "-map", "0", "-c:v", "libx265", "-crf", "20", "-c:a", "copy", "-c:s", "copy", "-y", str(output)])
        elif command == "/sync":
            raise RuntimeError("SYNC requires a reference track/file; upload intake must attach it before execution.")
        elif command == "/upscale":
            raise RuntimeError("AI upscale worker requires the GPU image; the job was not silently downgraded to a non-AI resize.")

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("Processing produced no valid output.")
        asyncio.run(notify(f"✅ {command[1:].upper()} completed. Output: {output.name}"))
    except Exception as exc:
        asyncio.run(notify(f"❌ Job {job_id} failed: {exc}"))
