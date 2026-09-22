from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

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
volume = modal.Volume.from_name("vikky-media", create_if_missing=True)

base_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "aria2", "git")
    .pip_install(
        "fastapi[standard]>=0.115,<1",
        "httpx>=0.28,<1",
        "numpy>=2.2,<3",
        "pydantic-settings>=2.7,<3",
        "pycdlib>=1.14,<2",
    )
    .add_local_dir("app", remote_path="/root/app")
)

gpu_image = (
    base_image
    .pip_install(
        "torch>=2.6,<3",
        "torchvision>=0.21,<1",
        "opencv-python-headless>=4.11,<5",
        "realesrgan>=0.3.0,<1",
        "basicsr>=1.4.2,<2",
    )
)

app = modal.App(APP_NAME)


def _tg_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _tg(token: str, method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20)) as client:
        r = await client.post(_tg_url(token, method), json=payload or {})
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"Telegram {method} failed"))
        return data


async def _send(token: str, chat_id: int, text: str) -> None:
    await _tg(token, "sendMessage", {"chat_id": chat_id, "text": text})


async def _telegram_download(token: str, file_id: str, destination: Path) -> Path:
    info = await _tg(token, "getFile", {"file_id": file_id})
    file_path = (info.get("result") or {}).get("file_path")
    file_size = (info.get("result") or {}).get("file_size", 0)
    if not file_path:
        raise RuntimeError("Telegram did not return a file path")
    if file_size and file_size > 20 * 1024 * 1024:
        raise RuntimeError(
            "Telegram Bot API download limit is 20 MiB. For multi-GB media, send a direct HTTPS download URL."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30)) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as out:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    out.write(chunk)
    return destination


async def _download_url(url: str, destination_dir: Path, progress) -> Path:
    from app.media.downloader import download_url
    progress("Downloading")
    return await download_url(url, destination_dir)


def _first_media(root: Path) -> Path:
    allowed = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts"}
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in allowed]
    if not files:
        raise RuntimeError("No supported media file found")
    return max(files, key=lambda p: p.stat().st_size)


def _validate_output(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError("Output file is missing or invalid")
    subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True, timeout=120,
    )


async def _run_job(job_id: str, chat_id: int, command: str, source_url: str,
                   reference_url: str | None = None) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    root = JOB_ROOT / job_id
    root.mkdir(parents=True, exist_ok=True)

    async def progress(stage: str) -> None:
        try:
            await _send(token, chat_id, f"🎬 {job_id}\n[{stage}]")
        except Exception:
            pass

    try:
        await progress("Downloading")
        source = await _download_url(source_url, root / "input", progress)

        if command == "/sync":
            if not reference_url:
                raise RuntimeError("SYNC requires two HTTPS URLs: candidate media and reference media.")
            reference = await _download_url(reference_url, root / "reference", progress)
            await progress("Syncing Audio/Subtitles")
            from app.sync.engine import sync_and_verify
            output = root / "Sync by Vikky.mkv"
            result = await asyncio.to_thread(sync_and_verify, reference, source, output)
            await progress(f"Sync verified (confidence={result.confidence:.2f})")

        elif command == "/encode":
            await progress("Encoding")
            from app.encode import encode_to_mkv
            output = root / "Vikky encoding.mkv"
            result = await asyncio.to_thread(
                encode_to_mkv, source, output, 3.0, 5.0, 3
            )
            await progress(
                f"Encoding verified ({result.actual_bytes / 1024**3:.2f} GiB, {result.codec})"
            )

        elif command == "/upscale":
            raise RuntimeError("Internal dispatch error: upscale must run on the GPU worker.")

        else:
            raise RuntimeError(f"Unsupported job type: {command}")

        _validate_output(output)
        await progress("Uploading")
        from app.uploaders import upload_to_all
        links = await upload_to_all(output)
        good = {name: link for name, link in links.items() if not link.startswith("ERROR:")}
        if not good:
            raise RuntimeError("All external upload providers failed: " + json.dumps(links))

        lines = ["✅ Job complete", f"📄 {output.name}"]
        for name, link in good.items():
            lines.append(f"• {name}: {link}")
        await _send(token, chat_id, "\n".join(lines))
    except Exception as exc:
        await _send(token, chat_id, f"❌ Job failed\n{type(exc).__name__}: {exc}")
        raise
    finally:
        # Keep only a small job record; large scratch/output files are not retained after delivery.
        try:
            for p in root.iterdir():
                if p.is_file():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            root.rmdir()
            volume.commit()
        except Exception:
            pass


@app.function(
    image=base_image,
    secrets=[telegram_secret],
    volumes={DATA_PATH: volume},
    cpu=8,
    memory=32768,
    timeout=7200,
    retries=1,
)
def cpu_worker(job_id: str, chat_id: int, command: str, source_url: str,
               reference_url: str | None = None):
    asyncio.run(_run_job(job_id, chat_id, command, source_url, reference_url))


@app.function(
    image=gpu_image,
    secrets=[telegram_secret],
    volumes={DATA_PATH: volume},
    gpu="A10G",
    cpu=8,
    memory=49152,
    timeout=7200,
    retries=1,
)
def upscale_worker(job_id: str, chat_id: int, source_url: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    root = JOB_ROOT / job_id
    root.mkdir(parents=True, exist_ok=True)

    async def notify(text: str):
        try:
            await _send(token, chat_id, text)
        except Exception:
            pass

    async def run():
        try:
            source = await _download_url(source_url, root / "input", lambda s: None)
            await notify(f"🎬 {job_id}\n[AI Upscaling]")
            from app.upscale import upscale_4k
            output = root / "Vikky AI Upscale 4K.mkv"
            result = await asyncio.to_thread(upscale_4k, source, output, root / "upscale-work")
            _validate_output(output)
            await notify("🎬 [Uploading]")
            from app.uploaders import upload_to_all
            links = await upload_to_all(output)
            good = {k: v for k, v in links.items() if not v.startswith("ERROR:")}
            if not good:
                raise RuntimeError("All external upload providers failed: " + json.dumps(links))
            await notify("✅ AI Upscale 4K complete\n" + "\n".join(f"• {k}: {v}" for k, v in good.items()))
        except Exception as exc:
            await notify(f"❌ Upscale failed\n{type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                if root.exists():
                    shutil.rmtree(root, ignore_errors=True)
                    volume.commit()
            except Exception:
                pass

    asyncio.run(run())


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
        return {"status": "ok", "service": APP_NAME, "telegram_configured": bool(token)}

    @web.get("/setup")
    async def setup(request: Request):
        url = str(request.base_url).rstrip("/") + "/webhook"
        await _tg(token, "setWebhook", {
            "url": url,
            "drop_pending_updates": False,
            "allowed_updates": ["message"],
        })
        return {"ok": True, "webhook": url}

    @web.get("/telegram/info")
    async def telegram_info():
        return await _tg(token, "getWebhookInfo")

    @web.post("/webhook")
    async def webhook(
        request: Request,
        secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ):
        update = await request.json()
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return {"ok": True}

        text = (message.get("text") or "").strip()
        parts = text.split()
        command = parts[0].lower() if parts else ""

        if command in {"/start", "/help"}:
            reply = (
                "🎬 Vikky Movie AI\n\n"
                "/status\n/queue\n"
                "/sync <candidate-URL> <reference-URL>\n"
                "/upscale <source-URL>\n"
                "/encode <source-URL>\n\n"
                "For multi-GB movies use direct HTTPS URLs; the standard Bot API cannot download 3–5 GiB files."
            )
        elif command == "/status":
            reply = "🟢 ONLINE\n☁️ Modal\n💾 Persistent Volume: ready\n⚙️ Workers: CPU + A10G GPU"
        elif command == "/queue":
            reply = "📦 Jobs are dispatched to Modal workers."
        elif command in {"/sync", "/encode", "/upscale"}:
            if command == "/sync" and len(parts) != 3:
                reply = "Usage: /sync <candidate-URL> <reference-URL>"
            elif command != "/sync" and len(parts) != 2:
                reply = f"Usage: {command} <source-URL>"
            elif any(urlparse(x).scheme not in {"http", "https"} for x in parts[1:]):
                reply = "Only HTTPS/HTTP media URLs are accepted."
            else:
                job_id = f"{chat_id}-{int(time.time() * 1000)}"
                if command == "/upscale":
                    upscale_worker.spawn(job_id, chat_id, parts[1])
                else:
                    cpu_worker.spawn(job_id, chat_id, command, parts[1], parts[2] if command == "/sync" else None)
                reply = f"✅ {command[1:].upper()} queued\n🆔 {job_id}"
        else:
            reply = "Use /help."

        await _send(token, chat_id, reply)
        return {"ok": True}

    @web.post("/jobs")
    async def submit(request: Request, authorization: str | None = Header(default=None)):
        if authorization != f"Bearer {remote_token}":
            raise HTTPException(status_code=401, detail="unauthorized")
        body = await request.json()
        job_id = str(body["job_id"])
        chat_id = int(body["chat_id"])
        command = str(body["job_type"])
        source_url = str(body["source_url"])
        if command == "upscale":
            upscale_worker.spawn(job_id, chat_id, source_url)
        else:
            cpu_worker.spawn(job_id, chat_id, "/" + command, source_url, body.get("reference_url"))
        return {"accepted": True, "job_id": job_id, "state": "queued"}

    return web
