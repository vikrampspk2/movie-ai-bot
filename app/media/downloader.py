from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

ARIA2_BIN = "aria2c"


class DownloadError(RuntimeError):
    pass


def _safe_name(url: str) -> str:
    name = Path(urlparse(url).path).name or "download.bin"
    return Path(name).name[:180]


async def download_url(url: str, destination_dir: Path) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DownloadError("Only http/https media URLs are supported")

    destination_dir.mkdir(parents=True, exist_ok=True)
    output = destination_dir / _safe_name(url)
    partial = output.with_suffix(output.suffix + ".aria2")

    if shutil.which(ARIA2_BIN) is None:
        raise DownloadError("aria2c is not installed on this worker")

    cmd = [
        ARIA2_BIN,
        "--allow-overwrite=false",
        "--auto-file-renaming=false",
        "--continue=true",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=4M",
        "--max-tries=8",
        "--retry-wait=2",
        "--timeout=30",
        "--connect-timeout=15",
        "--lowest-speed-limit=64K",
        "--file-allocation=none",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--check-integrity=true",
        "--dir", str(destination_dir),
        "--out", output.name,
        url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
    except OSError as exc:
        raise DownloadError(f"aria2c could not start: {exc}") from exc

    if process.returncode != 0:
        detail = (stderr or stdout).decode(errors="replace").strip()
        raise DownloadError(detail or "aria2c download failed")

    if partial.exists():
        raise DownloadError("aria2c left an incomplete download")
    if not output.exists() or output.stat().st_size == 0:
        raise DownloadError("Downloaded file is missing or empty")

    return output


async def download_url_with_fallback(url: str, destination_dir: Path) -> Path:
    try:
        return await download_url(url, destination_dir)
    except DownloadError:
        # The caller can report the failure; no alternate account/quota bypass is used.
        raise
