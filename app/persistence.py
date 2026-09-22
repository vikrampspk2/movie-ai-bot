from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Job, JobStatus, JobType


class JobStore:
    """Durable SQLite state for queued/running/completed jobs."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "jobs.sqlite3"
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )""")
            db.commit()

    def save(self, job: Job) -> None:
        payload = {
            "type": job.type.value, "source_name": job.source_name,
            "source_path": str(job.source_path) if job.source_path else None,
            "reference_path": str(job.reference_path) if job.reference_path else None,
            "id": job.id, "status": job.status.value, "stage": job.stage,
            "progress": job.progress, "backend": job.backend, "error": job.error,
            "created_at": job.created_at, "checkpoint": job.checkpoint,
            "audio_layout": job.audio_layout,
            "output_path": str(job.output_path) if job.output_path else None,
            "owner_id": job.owner_id, "upload_links": job.upload_links,
            "retry_count": job.retry_count, "eta_seconds": job.eta_seconds,
            "verified": job.verified, "upload_status": job.upload_status,
            "media_info": job.media_info,
        }
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs(id,payload,status,updated_at) VALUES(?,?,?,strftime('%s','now'))",
                (job.id, json.dumps(payload), job.status.value),
            )
            db.commit()

    def load_recoverable(self) -> list[Job]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM jobs WHERE status IN (?,?) ORDER BY updated_at",
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchall()
        result = []
        for row in rows:
            p = json.loads(row["payload"])
            p["type"] = JobType(p["type"])
            p["status"] = JobStatus.QUEUED
            if p.get("source_path"): p["source_path"] = Path(p["source_path"])
            if p.get("reference_path"): p["reference_path"] = Path(p["reference_path"])
            if p.get("output_path"): p["output_path"] = Path(p["output_path"])
            result.append(Job(**p))
        return result
