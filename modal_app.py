from __future__ import annotations

import os
import time
import modal

APP_NAME = os.getenv("MODAL_APP_NAME", "vikky-movie-ai")
PORT = 8000

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi>=0.115,<1", "uvicorn[standard]>=0.34,<1")
)

app = modal.App(APP_NAME)


@app.function(
    image=image,
    min_containers=1,
    max_containers=2,
    scaledown_window=300,
    retries=modal.Retries(max_retries=3, initial_delay=2, max_delay=30),
    timeout=120,
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel

    web = FastAPI(title="Vikky Modal Worker")

    class JobRequest(BaseModel):
        job_id: str
        job_type: str
        source_path: str | None = None
        reference_path: str | None = None
        workspace: str | None = None
        checkpoint: str | None = None

    @web.get("/health")
    async def health():
        return {"status": "ok", "service": "vikky-modal-worker", "time": time.time()}

    @web.post("/jobs")
    async def submit(job: JobRequest, authorization: str | None = Header(default=None)):
        expected = os.getenv("VIKKY_REMOTE_TOKEN")
        if not expected or authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="unauthorized")

        # Control-plane acknowledgement only. Media must be made available to
        # the Modal worker through shared/object storage before execution.
        return {
            "accepted": True,
            "job_id": job.job_id,
            "state": "queued",
            "execution": "not_started",
            "checkpoint": job.checkpoint,
        }

    return web
