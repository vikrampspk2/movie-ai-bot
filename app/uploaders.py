from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import httpx


class UploadError(RuntimeError):
    pass


async def upload_gofile(path: Path) -> str:
    # No API token: GoFile's upload endpoint creates a guest account/folder.
    async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=30.0)) as client:
        with path.open("rb") as handle:
            response = await client.post(
                "https://upload.gofile.io/uploadfile",
                files={"file": (path.name, handle, "application/octet-stream")},
            )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "ok":
            raise UploadError(f"GoFile upload failed: {data}")
        result = data.get("data") or {}
        link = result.get("downloadPage") or result.get("downloadUrl")
        if not link:
            raise UploadError("GoFile returned no download link")
        return link


async def upload_buzzheavier(path: Path) -> str:
    # Anonymous PUT upload; no account credential is required.
    endpoint = f"https://w.buzzheavier.com/{quote(path.name, safe='')}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=30.0)) as client:
        with path.open("rb") as handle:
            response = await client.put(endpoint, content=handle, headers={"Content-Type": "application/octet-stream"})
        response.raise_for_status()
        text = response.text.strip()
        if not text:
            raise UploadError("Buzzheavier returned an empty response")
        # API commonly returns the public file path/link as the response body.
        if text.startswith("http://") or text.startswith("https://"):
            return text
        try:
            data = response.json()
            link = data.get("url") or data.get("downloadUrl") or data.get("download")
            if link:
                return link
        except json.JSONDecodeError:
            pass
        if text.startswith("/"):
            return "https://buzzheavier.com" + text
        raise UploadError(f"Buzzheavier returned an unrecognized response: {text[:300]}")


async def upload_vikingfile(path: Path) -> str:
    # Anonymous multipart upload using VikingFile's documented fast multipart API.
    size = path.stat().st_size
    async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=30.0)) as client:
        meta = await client.post("https://vikingfile.com/api/get-upload-url", params={"size": size})
        meta.raise_for_status()
        info = meta.json()
        upload_id = info["uploadId"]
        key = info["key"]
        urls = info["urls"]
        parts: list[dict[str, object]] = []
        part_size = int(info["partSize"])

        with path.open("rb") as handle:
            for index, url in enumerate(urls, start=1):
                chunk = handle.read(part_size)
                if not chunk:
                    break
                response = await client.put(url, content=chunk, headers={"Content-Type": "application/octet-stream"})
                response.raise_for_status()
                etag = response.headers.get("ETag") or response.headers.get("etag")
                if not etag:
                    raise UploadError(f"VikingFile part {index} returned no ETag")
                parts.append({"PartNumber": index, "ETag": etag.strip('"')})

        payload: list[tuple[str, str]] = [("key", key), ("uploadId", upload_id), ("name", path.name), ("user", "")]
        for part in parts:
            payload.append(("parts[][PartNumber]", str(part["PartNumber"])))
            payload.append(("parts[][ETag]", str(part["ETag"])))

        complete = await client.post("https://vikingfile.com/api/complete-upload", data=payload)
        complete.raise_for_status()
        result = complete.json()
        link = result.get("url")
        if not link:
            raise UploadError(f"VikingFile returned no URL: {result}")
        return link


async def upload_to_all(path: Path) -> dict[str, str]:
    results: dict[str, str] = {}

    async def one(name: str, fn):
        try:
            results[name] = await fn(path)
        except Exception as exc:
            results[name] = f"ERROR: {exc}"

    # Independent services run concurrently so one slow host does not block the others.
    await asyncio.gather(
        one("GoFile (Guest)", upload_gofile),
        one("Buzzheavier (Anonymous)", upload_buzzheavier),
        one("VikingFile (Anonymous)", upload_vikingfile),
    )
    return results
