from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import httpx
import modal
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

APP_NAME = "vikky-movie-ai-bot"
DATA_DIR = Path("/data")
JOBS_DIR = DATA_DIR / "jobs"

app = modal.App(APP_NAME)

media_volume = modal.Volume.from_name("vikky-media", create_if_missing=True)
telegram_secret = modal.Secret.from_name(
    "vikky-telegram", required_keys=["TELEGRAM_BOT_TOKEN"]
)
remote_secret = modal.Secret.from_name(
    "vikky-remote", required_keys=["VIKKY_REMOTE_TOKEN"]
)

base_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "aria2", "git")
    .pip_install(
        "fastapi[standard]>=0.115,<1",
        "httpx>=0.28,<1",
        "numpy>=1.24,<3",
        "pydantic-settings>=2.7,<3",
        "pycdlib>=1.14,<2",
        "scipy>=1.11,<2",
        "soundfile>=0.12,<1",
        "requests>=2.31,<3",
        "psutil>=5.9,<8",
    )
    .add_local_python_source("app")
)

gpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "aria2", "libgl1", "libglib2.0-0")
    .pip_install(
        "torch>=2.2,<3",
        "torchvision>=0.17,<1",
        "torchaudio>=2.2,<3",
        "opencv-python-headless>=4.9,<5",
        "realesrgan>=0.3,<1",
        "basicsr>=1.4.2,<2",
        "fastapi[standard]>=0.115,<1",
        "httpx>=0.28,<1",
        "numpy>=1.24,<3",
        "psutil>=5.9,<8",
    )
    .add_local_python_source("app")
)


def run_coro(value):
    if not inspect.isawaitable(value):
        return value
    return asyncio.run(value)


def call_flexible(func, *args, **kwargs):
    return run_coro(func(*args, **kwargs))


def send_tg_message(chat_id: int, text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
            response.raise_for_status()
    except Exception as exc:
        print(f"Telegram notification error: {exc}", file=sys.stderr)


def verify_output(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError("Output file is missing or too small")
    subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def upload_all(path: Path) -> dict[str, str]:
    from app.uploaders import upload_to_all

    result = call_flexible(upload_to_all, path)
    if not isinstance(result, dict):
        raise RuntimeError("Uploader returned an invalid result")
    good = {k: v for k, v in result.items() if isinstance(v, str) and not v.startswith("ERROR:")}
    if not good:
        raise RuntimeError("All upload providers failed: " + str(result))
    return good


def format_links(links: dict[str, str]) -> str:
    return "\n".join(f"• {name}: {url}" for name, url in links.items())


def cleanup_scratch(*paths: Path) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


@app.function(
    image=base_image,
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_sync_task(job_id: str, chat_id: int, candidate_url: str, reference_url: str):
    media_volume.reload()
    job_dir = JOBS_DIR / job_id
    scratch = job_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    output = job_dir / "Sync by Vikky.mkv"
    processed = False

    try:
        from app.media.downloader import download_url
        from app.media.probe import probe
        from app.sync.engine import sync_and_verify

        send_tg_message(chat_id, f"🔄 *Job {job_id}*\n[Downloading] Candidate + reference...")
        candidate = run_coro(download_url(candidate_url, scratch))
        reference = run_coro(download_url(reference_url, scratch))

        send_tg_message(chat_id, f"🔍 *Job {job_id}*\n[Analyzing] Waveform/correlation/drift...")
        result = sync_and_verify(reference, candidate, output)
        verify_output(output)
        processed = True

        send_tg_message(chat_id, f"☁️ *Job {job_id}*\n[Uploading] Verified sync output...")
        links = upload_all(output)

        info = probe(output)
        layouts = ", ".join(
            f"{a.channels}ch {a.channel_layout or 'layout-unknown'}"
            for a in info.audio
        ) or "No audio"
        send_tg_message(
            chat_id,
            "✅ *Audio Sync Completed*\n\n"
            f"📄 `{output.name}`\n"
            f"🔊 *Audio:* {layouts}\n"
            f"📦 *Verification:* PASS\n\n"
            f"🔗 *Download Links:*\n{format_links(links)}",
        )
        output.unlink(missing_ok=True)
    except Exception as exc:
        print(f"SYNC {job_id}\n{traceback.format_exc()}", file=sys.stderr)
        if processed and output.exists():
            send_tg_message(
                chat_id,
                f"⚠️ *SYNC processed but upload failed*\nJob: `{job_id}`\n"
                f"Output retained for retry: `{output}`\nError: `{exc}`",
            )
        else:
            send_tg_message(chat_id, f"❌ *SYNC failed*\nJob: `{job_id}`\nError: `{exc}`")
    finally:
        cleanup_scratch(scratch)
        if job_dir.exists() and not output.exists():
            try:
                job_dir.rmdir()
            except OSError:
                pass
        media_volume.commit()


@app.function(
    image=base_image,
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_encode_task(job_id: str, chat_id: int, source_url: str):
    media_volume.reload()
    job_dir = JOBS_DIR / job_id
    scratch = job_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    output = job_dir / "Vikky encoding.mkv"
    processed = False

    try:
        from app.encode import encode_to_mkv
        from app.media.downloader import download_url
        from app.media.probe import probe

        send_tg_message(chat_id, f"📥 *Job {job_id}*\n[Downloading] Source...")
        source = run_coro(download_url(source_url, scratch))

        send_tg_message(chat_id, f"⚙️ *Job {job_id}*\n[Encoding] x265/NVENC target 3–5 GiB...")
        result = encode_to_mkv(source, output, 3.0, 5.0, 3)
        verify_output(output)
        processed = True

        send_tg_message(chat_id, f"☁️ *Job {job_id}*\n[Uploading] Verified encode...")
        links = upload_all(output)
        info = probe(output)
        layouts = ", ".join(
            f"{a.channels}ch {a.channel_layout or 'layout-unknown'}"
            for a in info.audio
        ) or "No audio"
        size_gib = output.stat().st_size / 1024**3

        send_tg_message(
            chat_id,
            "✅ *Encoding Completed*\n\n"
            f"📄 `{output.name}`\n"
            f"🎬 *Codec:* {result.codec}\n"
            f"🔊 *Audio:* {layouts}\n"
            f"💾 *Size:* {size_gib:.2f} GiB\n"
            f"📦 *Verification:* PASS\n\n"
            f"🔗 *Download Links:*\n{format_links(links)}",
        )
        output.unlink(missing_ok=True)
    except Exception as exc:
        print(f"ENCODE {job_id}\n{traceback.format_exc()}", file=sys.stderr)
        if processed and output.exists():
            send_tg_message(
                chat_id,
                f"⚠️ *ENCODE processed but upload failed*\nJob: `{job_id}`\n"
                f"Output retained for retry: `{output}`\nError: `{exc}`",
            )
        else:
            send_tg_message(chat_id, f"❌ *ENCODE failed*\nJob: `{job_id}`\nError: `{exc}`")
    finally:
        cleanup_scratch(scratch)
        if job_dir.exists() and not output.exists():
            try:
                job_dir.rmdir()
            except OSError:
                pass
        media_volume.commit()


@app.function(
    image=gpu_image,
    gpu="A10G",
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_upscale_task(job_id: str, chat_id: int, source_url: str):
    media_volume.reload()
    job_dir = JOBS_DIR / job_id
    scratch = job_dir / "scratch"
    workspace = job_dir / "upscale_workspace"
    scratch.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    output = job_dir / "Vikky AI Upscale 4K.mkv"
    processed = False

    try:
        from app.media.downloader import download_url
        from app.media.probe import probe
        from app.upscale import upscale_4k

        send_tg_message(chat_id, f"📥 *Job {job_id}*\n[Downloading] Source...")
        source = run_coro(download_url(source_url, scratch))

        send_tg_message(chat_id, f"🧠 *Job {job_id}*\n[AI Upscaling] Real-ESRGAN on A10G...")
        result = upscale_4k(source, output, workspace)
        verify_output(output)
        processed = True

        send_tg_message(chat_id, f"☁️ *Job {job_id}*\n[Uploading] Verified 4K output...")
        links = upload_all(output)
        info = probe(output)
        resolution = (
            f"{info.video.width}x{info.video.height}"
            if info.video else f"{result.get('width')}x{result.get('height')}"
        )

        send_tg_message(
            chat_id,
            "✅ *AI Upscaling Completed*\n\n"
            f"📄 `{output.name}`\n"
            f"🖥 *Resolution:* {resolution}\n"
            f"⚡ *GPU:* NVIDIA A10G\n"
            f"🧠 *Model:* {result.get('model', 'RealESRGAN_x4plus')}\n"
            f"📦 *Verification:* PASS\n\n"
            f"🔗 *Download Links:*\n{format_links(links)}",
        )
        output.unlink(missing_ok=True)
    except Exception as exc:
        print(f"UPSCALE {job_id}\n{traceback.format_exc()}", file=sys.stderr)
        if processed and output.exists():
            send_tg_message(
                chat_id,
                f"⚠️ *UPSCALE processed but upload failed*\nJob: `{job_id}`\n"
                f"Output retained for retry: `{output}`\nError: `{exc}`",
            )
        else:
            send_tg_message(chat_id, f"❌ *UPSCALE failed*\nJob: `{job_id}`\nError: `{exc}`")
    finally:
        cleanup_scratch(scratch, workspace)
        if job_dir.exists() and not output.exists():
            try:
                job_dir.rmdir()
            except OSError:
                pass
        media_volume.commit()


web_app = FastAPI(title="Vikky Movie AI Bot Control Plane")


@web_app.get("/health")
async def health():
    return {"status": "healthy", "service": APP_NAME}


@web_app.post("/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"status": "ignored"}, status_code=200)

    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return JSONResponse({"status": "ignored"}, status_code=200)

    chat_id = int(message["chat"]["id"])
    parts = message["text"].strip().split()
    command = parts[0].lower() if parts else ""

    if command in {"/start", "/help"}:
        send_tg_message(
            chat_id,
            "🤖 *Vikky Movie AI Bot*\n\n"
            "/sync <candidate-url> <reference-url>\n"
            "/upscale <source-url>\n"
            "/encode <source-url>\n"
            "/status\n/queue\n\n"
            "Multi-GB media uses direct HTTP/HTTPS intake.",
        )
        return {"status": "ok"}

    if command == "/status":
        send_tg_message(
            chat_id,
            "🟢 *ONLINE*\n"
            "☁️ Modal control plane\n"
            "💾 Persistent Volume: /data\n"
            "⚙️ CPU workers: 8 CPU / 32 GiB\n"
            "⚡ GPU worker: NVIDIA A10G / 32 GiB\n"
            "⏱ Worker timeout: 7200s",
        )
        return {"status": "ok"}

    job_id = os.urandom(6).hex()

    if command == "/sync":
        if len(parts) != 3 or not all(parts[i].startswith(("http://", "https://")) for i in (1, 2)):
            send_tg_message(chat_id, "❌ Usage: /sync <candidate-url> <reference-url>")
            return {"status": "invalid_args"}
        send_tg_message(chat_id, f"✅ *SYNC queued*\n🆔 `{job_id}`")
        process_sync_task.spawn(job_id, chat_id, parts[1], parts[2])
        return {"status": "queued", "job_id": job_id}

    if command in {"/encode", "/upscale"}:
        if len(parts) != 2 or not parts[1].startswith(("http://", "https://")):
            send_tg_message(chat_id, f"❌ Usage: {command} <source-url>")
            return {"status": "invalid_args"}
        if command == "/encode":
            process_encode_task.spawn(job_id, chat_id, parts[1])
        else:
            process_upscale_task.spawn(job_id, chat_id, parts[1])
        send_tg_message(chat_id, f"✅ *{command[1:].upper()} queued*\n🆔 `{job_id}`")
        return {"status": "queued", "job_id": job_id}

    return {"status": "ignored"}


@app.function(
    image=base_image,
    secrets=[telegram_secret, remote_secret],
    min_containers=1,
    max_containers=2,
    scaledown_window=300,
)
@modal.asgi_app()
def api():
    return web_app
