from __future__ import annotations

import os
import time
from pathlib import Path

import modal

APP_NAME = os.getenv("MODAL_APP_NAME", "vikky-movie-ai")
DATA_PATH = "/data"

volume = modal.Volume.from_name("vikky-media", create_if_missing=True)
remote_secret = modal.Secret.from_name(
    "vikky-remote",
    required_keys=["VIKKY_REMOTE_TOKEN"],
)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi>=0.115,<1",
        "uvicorn[standard]>=0.34,<1",
    )
)

app = modal.App(APP_NAME)


@app.function(
    image=image,
    secrets=[remote_secret],
    volumes={DATA_PATH: volume},
    min_containers=1,
    max_containers=2,
    scaledown_window=300,
    retries=modal.Retries(max_retries=3, initial_delay=2, max_delay=30),
    timeout=120,
    secrets=[modal.Secret.from_name("vikky-remote", required_keys=["VIKKY_REMOTE_TOKEN"])],
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel

    web = FastAPI(title="Vikky Modal Control Plane")

    class JobRequest(BaseModel):
        job_id: str
        job_type: str
        source_path: str | None = None
        reference_path: str | None = None
        workspace: str | None = None
        checkpoint: str | None = None

    def auth(authorization: str | None) -> None:
        expected = os.getenv("VIKKY_REMOTE_TOKEN")
        if not expected or authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="unauthorized")

    @web.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "vikky-modal-worker",
            "storage": DATA_PATH,
            "time": time.time(),
        }

    @web.post("/jobs")
    async def submit(
        job: JobRequest,
        authorization: str | None = Header(default=None),
    ):
        auth(authorization)
        job_dir = Path(DATA_PATH) / "jobs" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(
            job.model_dump_json(indent=2),
            encoding="utf-8",
        )
        volume.commit()
        return {
            "accepted": True,
            "job_id": job.job_id,
            "state": "queued",
            "workspace": str(job_dir),
            "checkpoint": job.checkpoint,
        }

    return web
