from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .probe import MediaInfo, probe
from .safe_extract import inspect_and_extract


@dataclass(slots=True)
class IntakeResult:
    source: Path
    media_files: list[Path]
    metadata: dict[str, MediaInfo]


def ingest(source: Path, workspace: Path) -> IntakeResult:
    media_files = inspect_and_extract(source, workspace)
    if not media_files:
        raise ValueError("No supported media file found in the supplied input")
    metadata = {str(path): probe(path) for path in media_files}
    return IntakeResult(source=source, media_files=media_files, metadata=metadata)
