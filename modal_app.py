from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

import httpx
import modal
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

APP_NAME = "vikky-movie-ai-bot"
DATA_DIR = Path("/data")
JOBS_DIR = DATA_DIR / "jobs"

app = modal.App(APP_NAME)

media_volume = modal.Volume.from_name(
    "vikky-media",
    create_if_missing=True,
)

telegram_secret = modal.Secret.from_name(
    "vikky-telegram",
    required_keys=["TELEGRAM_BOT_TOKEN"],
)

remote_secret = modal.Secret.from_name(
    "vikky-remote",
    required_keys=["VIKKY_REMOTE_TOKEN"],
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
    .apt_install(
        "ffmpeg",
        "aria2",
        "libgl1",
        "libglib2.0-0",
    )
    .pip_install(
        "torch==2.1.2",
        "torchvision==0.16.2",
        "torchaudio==2.1.2",
    )
    .pip_install(
        "opencv-python-headless>=4.9,<5",
        "numpy>=1.24,<3",
        "fastapi[standard]>=0.115,<1",
        "httpx>=0.28,<1",
        "psutil>=5.9,<8",
    )
    .pip_install(
        "basicsr>=1.4.2,<2",
        "realesrgan>=0.3,<1",
        extra_options="--no-build-isolation",
    )
    .add_local_python_source("app")
)


def run_async(awaitable: Any) -> Any:
    if not inspect.isawaitable(awaitable):
        return awaitable
    return asyncio.run(awaitable)


def call_flexible(func: Any, *args: Any, **kwargs: Any) -> Any:
    return run_async(func(*args, **kwargs))


def is_valid_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def send_tg_message(chat_id: int, text: str) -> None:
    """Send plain Telegram text; avoids Markdown failures from arbitrary URLs/errors."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not available", file=sys.stderr)
        return

    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "Telegram sendMessage failed"))
    except Exception as exc:
        print(f"Telegram notification failure: {exc}", file=sys.stderr)


def verify_output(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"Output file does not exist: {path}")
    if path.stat().st_size < 1024 * 1024:
        raise RuntimeError(f"Output file is suspiciously small (<1MB): {path}")

    try:
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe binary is missing from container") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffprobe verification failed: {exc.stderr.strip() or 'Corrupt media'}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe verification timed out") from exc


def cleanup_scratch(*paths: Path) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def upload_output(output_path: Path) -> dict[str, str]:
    from app.uploaders import upload_to_all

    result = call_flexible(upload_to_all, Path(output_path))

    if not isinstance(result, dict):
        raise RuntimeError("upload_to_all returned an invalid non-dictionary response")

    successful: dict[str, str] = {}
    for provider, value in result.items():
        if (
            isinstance(value, str)
            and value.strip()
            and not value.startswith("ERROR:")
        ):
            successful[str(provider)] = value.strip()

    if not successful:
        raise RuntimeError(f"All upload providers failed: {result}")

    return successful


def format_links(links: dict[str, str]) -> str:
    return "\n".join(f"• {name}: {url}" for name, url in links.items())


def audio_summary(media_info: Any) -> str:
    """Read audio information from MediaInfo.tracks without assuming direct fields."""
    tracks = getattr(media_info, "tracks", None)

    if tracks is not None:
        values: list[str] = []
        for track in tracks:
            track_type = getattr(
                track,
                "codec_type",
                getattr(track, "track_type", getattr(track, "type", getattr(track, "kind", None))),
            )
            if track_type is not None and str(track_type).lower() != "audio":
                continue

            channels = getattr(track, "channels", None)
            layout = getattr(
                track,
                "channel_layout",
                getattr(track, "layout", None),
            )
            sample_rate = getattr(track, "sample_rate", None)

            if channels is not None or layout is not None or sample_rate is not None:
                values.append(
                    f"{channels or '?'}ch {layout or 'layout-unknown'} "
                    f"{sample_rate or '?'}Hz"
                )

        if values:
            return ", ".join(values)

    return "Preserved"


def video_resolution(media_info: Any) -> str:
    """Extract video resolution by iterating MediaInfo.tracks."""
    tracks = getattr(media_info, "tracks", None)

    if tracks is None:
        return "Unknown"

    for track in tracks:
        track_type = getattr(
            track,
            "codec_type",
            getattr(track, "track_type", getattr(track, "type", getattr(track, "kind", None))),
        )

        if str(track_type).lower() != "video":
            continue

        width = getattr(track, "width", None)
        height = getattr(track, "height", None)

        if width and height:
            return f"{width}x{height}"

    return "Unknown"


def download_media(url: str, destination: Path) -> Path:
    """Run the repository's async download_url(url, path) contract safely."""
    from app.media.downloader import download_url

    destination.parent.mkdir(parents=True, exist_ok=True)
    result = run_async(download_url(url, destination))
    result_path = Path(result)

    if not result_path.exists() or result_path.stat().st_size <= 0:
        raise RuntimeError(
            f"Download produced an invalid or empty file: {result_path}"
        )

    return result_path


def commit_volume(job_id: str) -> None:
    try:
        media_volume.commit()
    except Exception as exc:
        print(
            f"Volume commit error on job {job_id}: {exc}",
            file=sys.stderr,
        )


def finish_job_dir(job_dir: Path, output_path: Path) -> None:
    if not output_path.exists() and job_dir.exists():
        try:
            job_dir.rmdir()
        except OSError:
            pass


@app.function(
    image=base_image,
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_sync_task(
    job_id: str,
    chat_id: int,
    candidate_url: str,
    reference_url: str,
):
    media_volume.reload()

    job_dir = JOBS_DIR / job_id
    scratch_dir = job_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    output_path = job_dir / "Sync by Vikky.mkv"
    processing_succeeded = False
    upload_succeeded = False

    try:
        from app.media.probe import probe
        from app.sync.engine import sync_and_verify

        send_tg_message(
            chat_id,
            f"🔄 SYNC — Job {job_id}\n\n[Downloading] Candidate + reference media...",
        )

        candidate_path = download_media(
            candidate_url,
            scratch_dir / "candidate",
        )
        reference_path = download_media(
            reference_url,
            scratch_dir / "reference",
        )

        send_tg_message(
            chat_id,
            f"🔍 SYNC — Job {job_id}\n\n[Analyzing] Waveforms, correlation, drift & layout...",
        )

        # REQUIRED repository order:
        # reference first, candidate second, output third.
        result = sync_and_verify(
            reference_path,
            candidate_path,
            output_path,
        )

        verify_output(output_path)
        processing_succeeded = True

        send_tg_message(
            chat_id,
            f"☁️ SYNC — Job {job_id}\n\n[Uploading] Verified sync output...",
        )

        links = upload_output(output_path)
        upload_succeeded = True

        media_info = probe(output_path)
        layouts = audio_summary(media_info)

        result_lines: list[str] = []
        confidence = getattr(result, "confidence", None)
        residual_offset = getattr(result, "estimated_offset_seconds", None)

        if confidence is not None:
            result_lines.append(f"🎯 Confidence: {confidence:.3f}")
        if residual_offset is not None:
            result_lines.append(
                f"⏱ Verified Offset: {residual_offset * 1000:.2f} ms"
            )

        result_text = (
            "\n".join(result_lines)
            if result_lines
            else "Sync verified successfully"
        )

        send_tg_message(
            chat_id,
            (
                "✅ Audio Sync Completed\n\n"
                f"📄 {output_path.name}\n"
                f"🔊 Audio: {layouts}\n"
                f"{result_text}\n"
                "📦 Verification: PASS\n\n"
                "🔗 Download Links:\n"
                f"{format_links(links)}"
            ),
        )

        # Delete output only after upload_output returned valid links.
        output_path.unlink(missing_ok=True)

    except Exception as exc:
        print(
            f"SYNC ERROR [{job_id}]\n{traceback.format_exc()}",
            file=sys.stderr,
        )

        if (
            processing_succeeded
            and not upload_succeeded
            and output_path.exists()
        ):
            send_tg_message(
                chat_id,
                (
                    "⚠️ SYNC processed — upload failed\n\n"
                    f"Job: {job_id}\n"
                    f"Output retained: {output_path}\n\n"
                    f"Error: {exc}"
                ),
            )
        else:
            send_tg_message(
                chat_id,
                (
                    "❌ SYNC failed\n\n"
                    f"Job: {job_id}\n"
                    f"Error: {exc}"
                ),
            )

    finally:
        cleanup_scratch(scratch_dir)
        finish_job_dir(job_dir, output_path)
        commit_volume(job_id)


@app.function(
    image=base_image,
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_encode_task(
    job_id: str,
    chat_id: int,
    source_url: str,
):
    media_volume.reload()

    job_dir = JOBS_DIR / job_id
    scratch_dir = job_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    output_path = job_dir / "Vikky encoding.mkv"
    processing_succeeded = False
    upload_succeeded = False

    try:
        from app.encode import encode_to_mkv
        from app.media.probe import probe

        send_tg_message(
            chat_id,
            f"📥 ENCODE — Job {job_id}\n\n[Downloading] Source movie...",
        )

        source_path = download_media(
            source_url,
            scratch_dir / "source",
        )

        send_tg_message(
            chat_id,
            f"⚙️ ENCODE — Job {job_id}\n\n[Encoding] High-efficiency target 3–5 GiB...",
        )

        result = encode_to_mkv(
            source_path,
            output_path,
            target_min_gb=3.0,
            target_max_gb=5.0,
            max_attempts=3,
        )

        verify_output(output_path)
        processing_succeeded = True

        send_tg_message(
            chat_id,
            f"🔍 ENCODE — Job {job_id}\n\n[Verifying] Container and streams...",
        )

        media_info = probe(output_path)

        send_tg_message(
            chat_id,
            f"☁️ ENCODE — Job {job_id}\n\n[Uploading] Verified encode...",
        )

        links = upload_output(output_path)
        upload_succeeded = True

        codec = getattr(result, "codec", "HEVC")
        size_gib = output_path.stat().st_size / (1024 ** 3)
        layouts = audio_summary(media_info)

        send_tg_message(
            chat_id,
            (
                "✅ Encoding Completed\n\n"
                f"📄 {output_path.name}\n"
                f"🎬 Codec: {codec}\n"
                f"🔊 Audio: {layouts}\n"
                f"💾 Size: {size_gib:.2f} GiB\n"
                "📦 Verification: PASS\n\n"
                "🔗 Download Links:\n"
                f"{format_links(links)}"
            ),
        )

        output_path.unlink(missing_ok=True)

    except Exception as exc:
        print(
            f"ENCODE ERROR [{job_id}]\n{traceback.format_exc()}",
            file=sys.stderr,
        )

        if (
            processing_succeeded
            and not upload_succeeded
            and output_path.exists()
        ):
            send_tg_message(
                chat_id,
                (
                    "⚠️ ENCODE processed — upload failed\n\n"
                    f"Job: {job_id}\n"
                    f"Output retained: {output_path}\n\n"
                    f"Error: {exc}"
                ),
            )
        else:
            send_tg_message(
                chat_id,
                (
                    "❌ ENCODE failed\n\n"
                    f"Job: {job_id}\n"
                    f"Error: {exc}"
                ),
            )

    finally:
        cleanup_scratch(scratch_dir)
        finish_job_dir(job_dir, output_path)
        commit_volume(job_id)


@app.function(
    image=gpu_image,
    gpu="T4",
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_upscale_task(
    job_id: str,
    chat_id: int,
    source_url: str,
):
    media_volume.reload()

    job_dir = JOBS_DIR / job_id
    scratch_dir = job_dir / "scratch"
    workspace_dir = job_dir / "upscale_workspace"

    scratch_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    output_path = job_dir / "Vikky AI Upscale 4K.mkv"
    processing_succeeded = False
    upload_succeeded = False

    try:
        from app.media.probe import probe
        from app.upscale import upscale_4k

        send_tg_message(
            chat_id,
            f"📥 UPSCALE — Job {job_id}\n\n[Downloading] Source for AI Upscale...",
        )

        source_path = download_media(
            source_url,
            scratch_dir / "source",
        )

        send_tg_message(
            chat_id,
            f"🧠 UPSCALE — Job {job_id}\n\n[AI Upscaling] Running Real-ESRGAN on NVIDIA A10G...",
        )

        # REQUIRED repository signature:
        # upscale_4k(input_path, output_path, workspace)
        upscale_4k(
            source_path,
            output_path,
            workspace_dir,
        )

        verify_output(output_path)
        processing_succeeded = True

        send_tg_message(
            chat_id,
            f"🔍 UPSCALE — Job {job_id}\n\n[Verifying] Probing 4K output...",
        )

        media_info = probe(output_path)
        resolution = video_resolution(media_info)
        layouts = audio_summary(media_info)

        send_tg_message(
            chat_id,
            f"☁️ UPSCALE — Job {job_id}\n\n[Uploading] 4K Upscaled output...",
        )

        links = upload_output(output_path)
        upload_succeeded = True

        size_gib = output_path.stat().st_size / (1024 ** 3)

        send_tg_message(
            chat_id,
            (
                "✅ AI Upscaling Completed\n\n"
                f"📄 {output_path.name}\n"
                f"🖥 Resolution: {resolution}\n"
                f"🔊 Audio: {layouts}\n"
                f"💾 Size: {size_gib:.2f} GiB\n"
                "⚡ Hardware: NVIDIA A10G\n"
                "📦 Verification: PASS\n\n"
                "🔗 Download Links:\n"
                f"{format_links(links)}"
            ),
        )

        output_path.unlink(missing_ok=True)

    except Exception as exc:
        print(
            f"UPSCALE ERROR [{job_id}]\n{traceback.format_exc()}",
            file=sys.stderr,
        )

        if (
            processing_succeeded
            and not upload_succeeded
            and output_path.exists()
        ):
            send_tg_message(
                chat_id,
                (
                    "⚠️ UPSCALE processed — upload failed\n\n"
                    f"Job: {job_id}\n"
                    f"Output retained: {output_path}\n\n"
                    f"Error: {exc}"
                ),
            )
        else:
            send_tg_message(
                chat_id,
                (
                    "❌ UPSCALE failed\n\n"
                    f"Job: {job_id}\n"
                    f"Error: {exc}"
                ),
            )

    finally:
        cleanup_scratch(scratch_dir, workspace_dir)
        finish_job_dir(job_dir, output_path)
        commit_volume(job_id)


web_app = FastAPI(title="Vikky Movie AI Bot Control Plane")


@web_app.get("/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": APP_NAME,
    }


@web_app.post("/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            status_code=200,
            content={"status": "ignored"},
        )

    message = data.get("message") or data.get("edited_message")

    if not message or "text" not in message:
        return JSONResponse(
            status_code=200,
            content={"status": "no text"},
        )

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()

    if not chat_id or not text:
        return JSONResponse(
            status_code=200,
            content={"status": "no text"},
        )

    parts = text.split()
    command = parts[0].lower()

    if command in {"/start", "/help"}:
        send_tg_message(
            chat_id,
            (
                "🤖 Vikky Movie AI Bot\n\n"
                "Commands:\n"
                "• /sync <candidate-url> <reference-url> — Waveform audio sync\n"
                "• /upscale <source-url> — Real-ESRGAN 4K AI Upscale (A10G GPU)\n"
                "• /encode <source-url> — High-efficiency x265 3–5 GiB encode\n"
                "• /status — Cluster & worker health\n"
                "• /queue — View retained job directories\n\n"
                "⚠️ Inputs must be direct HTTP/HTTPS download links."
            ),
        )
        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
        )

    if command == "/status":
        send_tg_message(
            chat_id,
            (
                "🟢 Cluster Status: Online\n\n"
                "• Backend: Modal Serverless\n"
                "• Storage: Persistent /data mounted\n"
                "• Workers: A10G GPU & 8-CPU nodes\n"
                "• Timeout: 7200s"
            ),
        )
        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
        )

    if command == "/queue":
        retained_jobs: list[str] = []

        if JOBS_DIR.exists():
            retained_jobs = sorted(
                p.name
                for p in JOBS_DIR.iterdir()
                if p.is_dir()
            )

        if retained_jobs:
            job_list = "\n".join(
                f"• {job_id}"
                for job_id in retained_jobs[:10]
            )
            msg = (
                "📋 Jobs currently retained in persistent storage\n"
                f"Total retained directories: {len(retained_jobs)}\n\n"
                f"{job_list}"
            )
        else:
            msg = (
                "📋 No job directories are currently retained "
                "in persistent storage."
            )

        send_tg_message(chat_id, msg)

        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
        )

    job_id = os.urandom(6).hex()

    if command == "/sync":
        if len(parts) != 3:
            send_tg_message(
                chat_id,
                "❌ Usage: /sync <candidate-url> <reference-url>\n"
                "Provide exactly two direct links.",
            )
            return JSONResponse(
                status_code=200,
                content={"status": "invalid args"},
            )

        candidate_url = parts[1]
        reference_url = parts[2]

        if not is_valid_url(candidate_url) or not is_valid_url(reference_url):
            send_tg_message(
                chat_id,
                "❌ Both candidate and reference must begin with "
                "http:// or https://",
            )
            return JSONResponse(
                status_code=200,
                content={"status": "invalid url scheme"},
            )

        send_tg_message(
            chat_id,
            (
                "✅ SYNC Queued\n"
                f"🆔 Job ID: {job_id}\n\n"
                "[Downloading candidates]"
            ),
        )

        process_sync_task.spawn(
            job_id,
            chat_id,
            candidate_url,
            reference_url,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "queued",
                "job_id": job_id,
            },
        )

    if command == "/encode":
        if len(parts) != 2:
            send_tg_message(
                chat_id,
                "❌ Usage: /encode <source-url>\n"
                "Provide exactly one direct link.",
            )
            return JSONResponse(
                status_code=200,
                content={"status": "invalid args"},
            )

        source_url = parts[1]

        if not is_valid_url(source_url):
            send_tg_message(
                chat_id,
                "❌ Source URL must begin with http:// or https://",
            )
            return JSONResponse(
                status_code=200,
                content={"status": "invalid url scheme"},
            )

        send_tg_message(
            chat_id,
            (
                "✅ ENCODE Queued\n"
                f"🆔 Job ID: {job_id}\n\n"
                "[Downloading source]"
            ),
        )

        process_encode_task.spawn(
            job_id,
            chat_id,
            source_url,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "queued",
                "job_id": job_id,
            },
        )

    if command == "/upscale":
        if len(parts) != 2:
            send_tg_message(
                chat_id,
                "❌ Usage: /upscale <source-url>\n"
                "Provide exactly one direct link.",
            )
            return JSONResponse(
                status_code=200,
                content={"status": "invalid args"},
            )

        source_url = parts[1]

        if not is_valid_url(source_url):
            send_tg_message(
                chat_id,
                "❌ Source URL must begin with http:// or https://",
            )
            return JSONResponse(
                status_code=200,
                content={"status": "invalid url scheme"},
            )

        send_tg_message(
            chat_id,
            (
                "✅ AI UPSCALE Queued\n"
                f"🆔 Job ID: {job_id}\n"
                "⚡ Hardware: A10G GPU\n\n"
                "[Downloading source]"
            ),
        )

        process_upscale_task.spawn(
            job_id,
            chat_id,
            source_url,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "queued",
                "job_id": job_id,
            },
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ignored"},
    )


@web_app.get("/jobs")
async def list_jobs(request: Request) -> JSONResponse:
    authorization = request.headers.get("Authorization", "")
    expected_token = os.environ.get("VIKKY_REMOTE_TOKEN", "")

    if not expected_token or authorization != f"Bearer {expected_token}":
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized"},
        )

    jobs_data: list[dict[str, Any]] = []

    if JOBS_DIR.exists():
        for path in sorted(JOBS_DIR.iterdir(), key=lambda p: p.name):
            if not path.is_dir():
                continue

            jobs_data.append(
                {
                    "job_id": path.name,
                    "retained_files": sorted(
                        file.name
                        for file in path.iterdir()
                        if file.is_file()
                    ),
                }
            )

    return JSONResponse(
        content={
            "status": "ok",
            "jobs": jobs_data,
        }
    )


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
