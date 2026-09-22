from __future__ import annotations

import io
import os
import shutil
import zipfile
from pathlib import Path

import pycdlib

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts", ".m2ts"}
MAX_FILES = 10000
MAX_SINGLE_FILE_BYTES = 20 * 1024**3
MAX_TOTAL_EXTRACTED_BYTES = 40 * 1024**3


def _safe_member_path(root: Path, name: str) -> Path:
    # ZIP names always use slash semantics, but normalize both separators.
    name = name.replace("\\", "/")
    candidate = (root / name).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"Unsafe archive path: {name!r}")
    return candidate


def safe_extract_zip(source: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted = 0
    total_bytes = 0
    media: list[Path] = []

    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise ValueError("Archive contains too many entries")

        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > MAX_SINGLE_FILE_BYTES:
                raise ValueError(f"Archive member is too large: {info.filename}")
            total_bytes += info.file_size
            if total_bytes > MAX_TOTAL_EXTRACTED_BYTES:
                raise ValueError("Archive expands beyond the extraction safety limit")

            target = _safe_member_path(destination, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            extracted += 1
            if target.suffix.lower() in MEDIA_EXTENSIONS:
                media.append(target)

    return media


def _iso_output_name(iso_path: str) -> str:
    # pycdlib returns ISO9660 names with ;1 version suffixes.
    name = iso_path.rsplit("/", 1)[-1]
    name = name.split(";", 1)[0]
    return name.strip(".") or "file"


def extract_media_from_iso(source: Path, destination: Path) -> list[Path]:
    """Extract media files from an ISO without mounting it.

    The ISO filesystem is traversed and only recognized media extensions are
    extracted. This avoids exposing the host filesystem to archive paths.
    """
    destination.mkdir(parents=True, exist_ok=True)
    iso = pycdlib.PyCdlib()
    media: list[Path] = []
    extracted_bytes = 0
    try:
        iso.open(str(source))
        for dirname, dirlist, filelist in iso.walk(iso_path="/"):
            for filename in filelist:
                clean_name = _iso_output_name(filename)
                if Path(clean_name).suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                if extracted_bytes >= MAX_TOTAL_EXTRACTED_BYTES:
                    raise ValueError("ISO media extraction safety limit reached")

                relative_dir = dirname.strip("/").replace(";1", "")
                target_dir = destination / relative_dir
                target_dir.mkdir(parents=True, exist_ok=True)
                target = _safe_member_path(destination, str(Path(relative_dir) / clean_name))

                with target.open("wb") as out:
                    iso.get_file_from_iso_fp(out, iso_path=f"{dirname.rstrip('/')}/{filename}")
                size = target.stat().st_size
                extracted_bytes += size
                if size > MAX_SINGLE_FILE_BYTES or extracted_bytes > MAX_TOTAL_EXTRACTED_BYTES:
                    target.unlink(missing_ok=True)
                    raise ValueError("ISO media extraction safety limit exceeded")
                media.append(target)
    finally:
        iso.close()
    return media


def discover_media(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
    )


def inspect_and_extract(source: Path, workspace: Path) -> list[Path]:
    """Normalize a source into media paths inside a controlled workspace."""
    workspace.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".zip":
        extracted = safe_extract_zip(source, workspace / "zip")
        # ZIPs may contain nested ZIP/ISO files.
        for nested in list(discover_media(workspace / "zip")):
            if nested.suffix.lower() in MEDIA_EXTENSIONS and nested not in extracted:
                extracted.append(nested)
        for archive in (workspace / "zip").rglob("*.iso"):
            extracted.extend(extract_media_from_iso(archive, workspace / "iso"))
        return sorted(set(extracted + discover_media(workspace)))
    if source.suffix.lower() == ".iso":
        return extract_media_from_iso(source, workspace / "iso")
    if source.suffix.lower() in MEDIA_EXTENSIONS:
        target = workspace / source.name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return [target]
    raise ValueError(f"Unsupported input type: {source.suffix or 'unknown'}")
