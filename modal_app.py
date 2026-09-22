from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def tg_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not available")
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"Telegram {method} failed"))
    return data


def send_tg_message(
    chat_id: int,
    text: str,
    reply_keyboard: bool = False,
) -> int | None:
    keyboard = {
        "keyboard": [
            ["🔄 Audio Sync", "🎬 Hybrid Remaster (1080p)"],
            ["🧠 4K AI Upscale", "📊 Cluster Status"],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
    }
    try:
        data = tg_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                **({"reply_markup": keyboard} if reply_keyboard else {}),
            },
        )
        return int((data.get("result") or {}).get("message_id"))
    except Exception as exc:
        print(f"Telegram sendMessage failure: {exc}", file=sys.stderr)
        return None


def edit_tg_message(chat_id: int, message_id: int | None, text: str) -> None:
    if not message_id:
        return
    try:
        tg_api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            },
        )
    except Exception as exc:
        print(f"Telegram editMessageText failure: {exc}", file=sys.stderr)


def send_status(chat_id: int, message_id: int | None, text: str) -> None:
    edit_tg_message(chat_id, message_id, text)


USER_STATE_FILE = DATA_DIR / "user_state.json"


def load_user_states() -> dict[str, dict[str, Any]]:
    try:
        if USER_STATE_FILE.exists():
            raw = json.loads(USER_STATE_FILE.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        print(f"State load error: {exc}", file=sys.stderr)
    return {}


def save_user_states(states: dict[str, dict[str, Any]]) -> None:
    USER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USER_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(states, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, USER_STATE_FILE)


def set_user_state(chat_id: int, state: dict[str, Any]) -> None:
    states = load_user_states()
    states[str(chat_id)] = state
    save_user_states(states)


def get_user_state(chat_id: int) -> dict[str, Any] | None:
    return load_user_states().get(str(chat_id))


def clear_user_state(chat_id: int) -> None:
    states = load_user_states()
    states.pop(str(chat_id), None)
    save_user_states(states)


def audio_summary(media_info: Any) -> str:
    tracks = getattr(media_info, "tracks", None)
    if tracks is not None:
        values: list[str] = []
        for track in tracks:
            track_type = getattr(
                track,
                "codec_type",
                getattr(track, "track_type", getattr(track, "type", getattr(track, "kind", None))),
            )
            if str(track_type).lower() != "audio":
                continue
            channels = getattr(track, "channels", None)
            layout = getattr(track, "channel_layout", getattr(track, "layout", None))
            sample_rate = getattr(track, "sample_rate", None)
            codec = getattr(track, "codec_name", getattr(track, "codec", None))
            if channels is not None or layout is not None:
                values.append(
                    f"{codec or '?'} {channels or '?'}ch {layout or 'layout-unknown'} "
                    f"{sample_rate or '?'}Hz"
                )
        if values:
            return ", ".join(values)
    return "Preserved"


def video_resolution(media_info: Any) -> str:
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


def verify_output(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 1024 * 1024:
        raise RuntimeError(f"Invalid output: {path}")
    subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def cleanup_scratch(*paths: Path) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def upload_output(output_path: Path) -> dict[str, str]:
    from app.uploaders import upload_to_all
    result = call_flexible(upload_to_all, Path(output_path))
    if not isinstance(result, dict):
        raise RuntimeError("upload_to_all returned an invalid response")
    successful = {
        str(provider): value.strip()
        for provider, value in result.items()
        if isinstance(value, str) and value.strip() and not value.startswith("ERROR:")
    }
    if not successful:
        raise RuntimeError(f"All upload providers failed: {result}")
    return successful


def format_links(links: dict[str, str]) -> str:
    return "\n".join(f"• {name}: {url}" for name, url in links.items())


def commit_volume(job_id: str) -> None:
    try:
        media_volume.commit()
    except Exception as exc:
        print(f"Volume commit error [{job_id}]: {exc}", file=sys.stderr)


def finish_job_dir(job_dir: Path, output_path: Path) -> None:
    if not output_path.exists() and job_dir.exists():
        try:
            job_dir.rmdir()
        except OSError:
            pass


MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts",
    ".mts", ".mp3", ".aac", ".m4a", ".flac", ".wav", ".ogg", ".opus",
}


def is_media_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS


def safe_extract_zip(zip_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        total_uncompressed = sum(max(0, i.file_size) for i in infos)
        if total_uncompressed > 60 * 1024**3 or len(infos) > 10000:
            raise RuntimeError("ZIP exceeds safe extraction limits")
        for info in infos:
            target = (destination / info.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("Unsafe ZIP path traversal detected")
            if info.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

    candidates = [p for p in destination.rglob("*") if is_media_file(p)]
    if not candidates:
        raise RuntimeError("ZIP contains no supported media file")
    # Prefer the largest media file as the primary movie/audio source.
    primary = max(candidates, key=lambda p: p.stat().st_size)
    zip_path.unlink(missing_ok=True)
    return primary


def maybe_extract_archive(path: Path, workspace: Path) -> Path:
    if path.suffix.lower() == ".zip":
        return safe_extract_zip(path, workspace / "unzipped")
    return path


def aria2_download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "aria2c",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        "--max-connection-per-server=8",
        "--split=8",
        "--min-split-size=4M",
        "--file-allocation=none",
        "--dir", str(destination.parent),
        "--out", destination.name,
        url,
    ]
    subprocess.run(command, check=True, timeout=7200)
    if not destination.exists() or destination.stat().st_size <= 0:
        raise RuntimeError("aria2c produced no file")
    return destination


def _html_download_link(url: str, html: str) -> str | None:
    # Prefer explicit download attributes/links, then common direct-file hrefs.
    patterns = [
        r'href=["\']([^"\']+)["\'][^>]*(?:download|Download)',
        r'(?:download|Download)[^<]{0,120}href=["\']([^"\']+)["\']',
        r'href=["\']([^"\']+\.(?:mkv|mp4|m4v|mov|ts|m2ts|mp3|flac|wav|zip)(?:\?[^"\']*)?)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return urljoin(url, unquote(match.group(1)))
    return None


def resolve_gofile(url: str) -> str:
    match = re.search(r"/d/([A-Za-z0-9]+)", url)
    if not match:
        return url
    file_id = match.group(1)
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        api = client.get(f"https://api.gofile.io/contents/{file_id}")
        if api.is_success:
            payload = api.json()
            data = payload.get("data") or {}
            direct = data.get("link")
            if isinstance(direct, str) and direct.startswith("http"):
                return direct
            contents = data.get("children") or {}
            if isinstance(contents, dict):
                files = [v for v in contents.values() if isinstance(v, dict) and v.get("link")]
                if files:
                    return str(max(files, key=lambda x: x.get("size", 0))["link"])
    return url


def resolve_platform_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "gofile.io" in host:
        return resolve_gofile(url)

    # PixelDrain's /api/file/{id} endpoint is already a direct file endpoint.
    match = re.search(r"pixeldrain\.com/(?:u|l)/([A-Za-z0-9_-]+)", url)
    if match:
        return f"https://pixeldrain.com/api/file/{match.group(1)}"

    # Buzzheavier and GDFlix landing pages are resolved by following their
    # download link; direct HTTP links are left untouched.
    if "buzzheavier.com" in host or "gdflix" in host:
        with httpx.Client(timeout=45, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            direct = _html_download_link(str(response.url), response.text)
            if direct:
                return direct

    return url


def download_media(url: str, destination: Path) -> Path:
    if not is_valid_url(url):
        raise ValueError("Invalid HTTP/HTTPS media URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolve_platform_url(url)
    downloaded = aria2_download(resolved, destination)
    return maybe_extract_archive(downloaded, destination.parent)


def extract_reference_audio(reference_path: Path, scratch_dir: Path) -> Path:
    # Pure audio sources are used directly.
    if reference_path.suffix.lower() in {".flac", ".wav", ".m4a", ".aac", ".mp3", ".ogg", ".opus"}:
        return reference_path

    # For video containers, select the highest-quality multi-channel audio stream.
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries",
            "stream=index,codec_name,channels,channel_layout,bit_rate:stream_tags=language",
            "-of", "json", str(reference_path),
        ],
        check=True, capture_output=True, text=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("Reference source contains no audio stream")

    preferred = {"dts": 100, "truehd": 95, "flac": 90, "eac3": 85, "ac3": 80}
    def score(stream: dict[str, Any]) -> tuple[int, int, int]:
        codec = str(stream.get("codec_name") or "").lower()
        channels = int(stream.get("channels") or 0)
        bitrate = int(stream.get("bit_rate") or 0)
        return (preferred.get(codec, 10) + min(channels, 8) * 5, channels, bitrate)

    best = max(streams, key=score)
    stream_index = best["index"]
    output = scratch_dir / "reference_audio.flac"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(reference_path),
            "-map", f"0:{stream_index}",
            "-vn",
            "-c:a", "flac",
            str(output),
        ],
        check=True, timeout=7200,
    )
    return output


def run_ffmpeg_remaster(source_path: Path, output_path: Path) -> None:
    # The requested visual profile is implemented as a single deterministic
    # filtergraph, with CFR output and 10-bit HEVC.
    vf = (
        "scale=1920:1080:flags=lanczos,"
        "unsharp=5:5:0.8:5:5:0.0,"
        "deband=1:64:16:16,"
        "eq=saturation=1.15:contrast=1.05:brightness=0.01,"
        "hqdn3d=1.2:1.2:3:3,"
        "gradfun=1.0:16,"
        "vibrance=intensity=0.08,"
        "format=yuv420p10le"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(source_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map", "0:s?",
            "-vf", vf,
            "-c:v", "libx265",
            "-crf", "19",
            "-preset", "medium",
            "-pix_fmt", "yuv420p10le",
            "-fps_mode", "cfr",
            "-c:a", "copy",
            "-c:s", "copy",
            "-max_muxing_queue_size", "4096",
            str(output_path),
        ],
        check=True,
        timeout=7200,
    )


def run_encode_worker(job_id: str, chat_id: int, source_url: str) -> None:
    process_encode_task(job_id, chat_id, source_url)


def job_workspace(job_id: str) -> tuple[Path, Path]:
    job_dir = JOBS_DIR / job_id
    scratch = job_dir / "scratch"
    job_dir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return job_dir, scratch


@app.function(
    image=base_image,
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_sync_task(
    chat_id: int,
    candidate_url: str,
    audio_url: str,
):
    media_volume.reload()
    job_id = os.urandom(6).hex()
    job_dir, scratch_dir = job_workspace(job_id)
    output_path = job_dir / "Sync by Vikky.mkv"
    status_id = send_tg_message(chat_id, "[1/4] 📥 Downloading via turbo engine...")
    processing_succeeded = False
    upload_succeeded = False

    try:
        from app.media.probe import probe
        from app.sync.engine import sync_and_verify

        candidate_path = download_media(candidate_url, scratch_dir / "candidate")
        reference_path = download_media(audio_url, scratch_dir / "reference")
        send_status(chat_id, status_id, "[2/4] 🔍 Inspecting media & extracting reference audio...")
        reference_audio = extract_reference_audio(reference_path, scratch_dir)

        send_status(chat_id, status_id, "[3/4] ⚙️ Processing (Waveform Sync / 15-Filter Remaster)...")
        # Contract: reference first, candidate second, output third.
        result = sync_and_verify(reference_audio, candidate_path, output_path)
        verify_output(output_path)
        processing_succeeded = True

        send_status(chat_id, status_id, "[4/4] ☁️ Uploading output to cloud hosts...")
        links = upload_output(output_path)
        upload_succeeded = True

        info = probe(output_path)
        confidence = getattr(result, "confidence", None)
        offset = getattr(result, "estimated_offset_seconds", None)
        details = []
        if confidence is not None:
            details.append(f"🎯 Confidence: {confidence:.3f}")
        if offset is not None:
            details.append(f"⏱ Verified Offset: {offset * 1000:.2f} ms")
        details_text = "\n".join(details)
        final = (
            "✅ Process Completed!\n\n"
            f"📄 File: {output_path.name}\n"
            f"🔊 Audio: {audio_summary(info)}\n"
            f"{details_text + chr(10) if details_text else ''}"
            "📦 Verification: PASS\n"
            "🔗 Download Links:\n"
            f"{format_links(links)}"
        )
        send_status(chat_id, status_id, final)
        output_path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"SYNC ERROR [{job_id}]\n{traceback.format_exc()}", file=sys.stderr)
        if processing_succeeded and not upload_succeeded and output_path.exists():
            send_status(chat_id, status_id, f"⚠️ Processed but upload failed.\n\nOutput retained: {output_path.name}\nError: {exc}")
        else:
            send_status(chat_id, status_id, f"❌ Audio Sync failed.\n\nJob: {job_id}\nError: {exc}")
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
    chat_id: int,
    source_url: str,
):
    media_volume.reload()
    job_id = os.urandom(6).hex()
    job_dir, scratch_dir = job_workspace(job_id)
    output_path = job_dir / "Vikky encoding.mkv"
    status_id = send_tg_message(chat_id, "[1/4] 📥 Downloading via turbo engine...")
    processing_succeeded = False
    upload_succeeded = False

    try:
        send_status(chat_id, status_id, "[2/4] 🔍 Inspecting media & extracting reference audio...")
        source_path = download_media(source_url, scratch_dir / "source")
        send_status(chat_id, status_id, "[3/4] ⚙️ Processing (Waveform Sync / 15-Filter Remaster)...")
        run_ffmpeg_remaster(source_path, output_path)
        verify_output(output_path)
        processing_succeeded = True
        send_status(chat_id, status_id, "[4/4] ☁️ Uploading output to cloud hosts...")
        links = upload_output(output_path)
        upload_succeeded = True
        final = (
            "✅ Process Completed!\n\n"
            f"📄 File: {output_path.name}\n"
            "🎬 Profile: Hybrid Remaster 1080p / HEVC 10-bit\n"
            "🔊 Audio: Original layout preserved\n"
            "📦 Verification: PASS\n"
            "🔗 Download Links:\n"
            f"{format_links(links)}"
        )
        send_status(chat_id, status_id, final)
        output_path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"REMASTER ERROR [{job_id}]\n{traceback.format_exc()}", file=sys.stderr)
        if processing_succeeded and not upload_succeeded and output_path.exists():
            send_status(chat_id, status_id, f"⚠️ Remaster processed but upload failed.\n\nOutput retained: {output_path.name}\nError: {exc}")
        else:
            send_status(chat_id, status_id, f"❌ Hybrid Remaster failed.\n\nJob: {job_id}\nError: {exc}")
    finally:
        cleanup_scratch(scratch_dir)
        finish_job_dir(job_dir, output_path)
        commit_volume(job_id)


@app.function(
    image=gpu_image,
    volumes={str(DATA_DIR): media_volume},
    secrets=[telegram_secret, remote_secret],
    cpu=8,
    memory=32768,
    timeout=7200,
)
def process_upscale_task(
    chat_id: int,
    source_url: str,
):
    media_volume.reload()
    job_id = os.urandom(6).hex()
    job_dir, scratch_dir = job_workspace(job_id)
    workspace_dir = job_dir / "upscale_workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "Vikky AI Upscale 4K.mkv"
    status_id = send_tg_message(chat_id, "[1/4] 📥 Downloading via turbo engine...")
    processing_succeeded = False
    upload_succeeded = False

    try:
        from app.media.probe import probe
        from app.upscale import upscale_4k

        source_path = download_media(source_url, scratch_dir / "source")
        send_status(chat_id, status_id, "[2/4] 🔍 Inspecting media & extracting reference audio...")
        send_status(chat_id, status_id, "[3/4] ⚙️ Processing (Waveform Sync / 15-Filter Remaster)...")
        upscale_4k(source_path, output_path, workspace_dir)
        verify_output(output_path)
        processing_succeeded = True
        send_status(chat_id, status_id, "[4/4] ☁️ Uploading output to cloud hosts...")
        links = upload_output(output_path)
        upload_succeeded = True
        info = probe(output_path)
        final = (
            "✅ Process Completed!\n\n"
            f"📄 File: {output_path.name}\n"
            f"🖥 Resolution: {video_resolution(info)}\n"
            f"🔊 Audio: {audio_summary(info)}\n"
            "🧠 AI: Real-ESRGAN CPU worker\n"
            "📦 Verification: PASS\n"
            "🔗 Download Links:\n"
            f"{format_links(links)}"
        )
        send_status(chat_id, status_id, final)
        output_path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"UPSCALE ERROR [{job_id}]\n{traceback.format_exc()}", file=sys.stderr)
        if processing_succeeded and not upload_succeeded and output_path.exists():
            send_status(chat_id, status_id, f"⚠️ Upscale processed but upload failed.\n\nOutput retained: {output_path.name}\nError: {exc}")
        else:
            send_status(chat_id, status_id, f"❌ 4K AI Upscale failed.\n\nJob: {job_id}\nError: {exc}")
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
        return JSONResponse(status_code=200, content={"status": "ignored"})

    message = data.get("message") or data.get("edited_message")
    if not message or "text" not in message:
        return JSONResponse(status_code=200, content={"status": "no text"})

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()
    if not chat_id or not text:
        return JSONResponse(status_code=200, content={"status": "no text"})

    if text in {"/start", "/help"}:
        send_tg_message(
            chat_id,
            "🤖 Vikky Movie AI Bot\n\nChoose an operation below. I will guide you step-by-step.",
            reply_keyboard=True,
        )
        return JSONResponse(status_code=200, content={"status": "ok"})

    if text == "📊 Cluster Status" or text == "/status":
        send_tg_message(
            chat_id,
            "🟢 Cluster Status: Online\n\n"
            "• Backend: Modal Serverless\n"
            "• Storage: Persistent /data mounted\n"
            "• Workers: 8 CPU / 32 GB RAM\n"
            "• GPU: Disabled (free-tier deployment)\n"
            "• Timeout: 7200s",
            reply_keyboard=True,
        )
        return JSONResponse(status_code=200, content={"status": "ok"})

    if text == "🔄 Audio Sync":
        set_user_state(chat_id, {"action": "sync", "step": "awaiting_video"})
        send_tg_message(
            chat_id,
            "📥 Step 1/2: Please send the Main Video Source (Candidate Video) link:",
            reply_keyboard=True,
        )
        return JSONResponse(status_code=200, content={"status": "awaiting_video"})

    if text == "🎬 Hybrid Remaster (1080p)":
        set_user_state(chat_id, {"action": "remaster", "step": "awaiting_video"})
        send_tg_message(
            chat_id,
            "📥 Please send the Video link to Remaster & Encode (1080p x265):",
            reply_keyboard=True,
        )
        return JSONResponse(status_code=200, content={"status": "awaiting_video"})

    if text == "🧠 4K AI Upscale":
        set_user_state(chat_id, {"action": "upscale", "step": "awaiting_video"})
        send_tg_message(
            chat_id,
            "📥 Please send the Video link for CPU-based 4K AI Upscale:",
            reply_keyboard=True,
        )
        return JSONResponse(status_code=200, content={"status": "awaiting_video"})

    state = get_user_state(chat_id)

    if state:
        if not is_valid_url(text):
            send_tg_message(
                chat_id,
                "❌ Please send a valid HTTP/HTTPS media link.",
                reply_keyboard=True,
            )
            return JSONResponse(status_code=200, content={"status": "invalid url"})

        action = state.get("action")
        step = state.get("step")

        if action == "sync" and step == "awaiting_video":
            set_user_state(
                chat_id,
                {
                    "action": "sync",
                    "step": "awaiting_audio",
                    "candidate_url": text,
                },
            )
            send_tg_message(
                chat_id,
                "🎵 Step 2/2: Now send the Audio Source link (or a Reference Video containing the audio):",
                reply_keyboard=True,
            )
            return JSONResponse(status_code=200, content={"status": "awaiting_audio"})

        if action == "sync" and step == "awaiting_audio":
            candidate_url = str(state["candidate_url"])
            clear_user_state(chat_id)
            process_sync_task.spawn(chat_id, candidate_url, text)
            return JSONResponse(status_code=200, content={"status": "queued"})

        if action == "remaster" and step == "awaiting_video":
            clear_user_state(chat_id)
            process_encode_task.spawn(chat_id, text)
            return JSONResponse(status_code=200, content={"status": "queued"})

        if action == "upscale" and step == "awaiting_video":
            clear_user_state(chat_id)
            process_upscale_task.spawn(chat_id, text)
            return JSONResponse(status_code=200, content={"status": "queued"})

    # Keep legacy commands from being multi-argument: guide the user back to the menu.
    if text.startswith("/"):
        send_tg_message(
            chat_id,
            "Use the menu buttons above for the step-by-step workflow.",
            reply_keyboard=True,
        )
    else:
        send_tg_message(
            chat_id,
            "Choose an operation from the menu above.",
            reply_keyboard=True,
        )

    return JSONResponse(status_code=200, content={"status": "ignored"})


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
