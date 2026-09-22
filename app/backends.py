from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings
from .models import Job, JobType


@dataclass(frozen=True, slots=True)
class Backend:
    name: str
    enabled: bool
    endpoint: str
    api_key: str
    supports_gpu: bool = True
    supports_cpu: bool = True


class BackendSelector:
    """Failover order: Lightning -> Modal -> Kaggle.

    A provider is usable only when its endpoint and credential are configured.
    No fake/local success is reported for an unavailable cloud backend.
    """
    def __init__(self) -> None:
        self.backends = (
            Backend("lightning", settings.lightning_enabled, settings.lightning_endpoint, settings.lightning_api_key),
            Backend("modal", settings.modal_enabled, settings.modal_endpoint, settings.modal_api_key),
            Backend("kaggle", settings.kaggle_enabled, settings.kaggle_endpoint, settings.kaggle_api_key),
        )

    def candidates(self, job: Job) -> list[Backend]:
        gpu_required = job.type is JobType.UPSCALE and settings.gpu_required_for_upscale
        return [b for b in self.backends if b.enabled and b.endpoint and b.api_key and ((not gpu_required) or b.supports_gpu)]

    def select(self, job: Job, excluded: set[str] | None = None) -> Backend | None:
        excluded = excluded or set()
        return next((b for b in self.candidates(job) if b.name not in excluded), None)


class BackendError(RuntimeError):
    pass


async def dispatch_remote(backend: Backend, job: Job) -> dict[str, Any]:
    payload = {
        "job_id": job.id, "job_type": job.type.value,
        "source_path": str(job.source_path) if job.source_path else None,
        "reference_path": str(job.reference_path) if job.reference_path else None,
        "workspace": str(job.source_path.parent) if job.source_path else None,
        "checkpoint": job.checkpoint,
    }
    headers = {"Authorization": f"Bearer {backend.api_key}", "X-Vikky-Job-ID": job.id}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.post(backend.endpoint.rstrip("/") + "/jobs", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not data.get("accepted", False):
                raise BackendError(f"{backend.name} rejected job")
            return data
    except Exception as exc:
        raise BackendError(f"{backend.name} dispatch failed: {exc}") from exc


backend_selector = BackendSelector()
