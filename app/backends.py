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
    """Production order: Modal first, Kaggle second.

    A backend is considered usable only when its endpoint and credential are
    configured. GPU-required jobs never fall back to a CPU backend.
    """

    def __init__(self) -> None:
        self.backends = (
            Backend(
                "modal",
                settings.modal_enabled,
                settings.modal_endpoint,
                settings.modal_api_key,
                supports_gpu=True,
                supports_cpu=True,
            ),
            Backend(
                "kaggle",
                settings.kaggle_enabled,
                settings.kaggle_endpoint,
                settings.kaggle_api_key,
                supports_gpu=True,
                supports_cpu=True,
            ),
        )

    def candidates(self, job: Job) -> list[Backend]:
        gpu_required = job.type is JobType.UPSCALE and settings.gpu_required_for_upscale
        return [
            backend
            for backend in self.backends
            if backend.enabled
            and backend.endpoint
            and backend.api_key
            and (not gpu_required or backend.supports_gpu)
        ]

    def select(self, job: Job, excluded: set[str] | None = None) -> Backend | None:
        excluded = excluded or set()
        return next(
            (backend for backend in self.candidates(job) if backend.name not in excluded),
            None,
        )


class BackendError(RuntimeError):
    pass


async def dispatch_remote(backend: Backend, job: Job) -> dict[str, Any]:
    payload = {
        "job_id": job.id,
        "job_type": job.type.value,
        "source_path": str(job.source_path) if job.source_path else None,
        "reference_path": str(job.reference_path) if job.reference_path else None,
        "workspace": str(job.source_path.parent) if job.source_path else None,
        "checkpoint": job.checkpoint,
    }
    headers = {
        "Authorization": f"Bearer {backend.api_key}",
        "X-Vikky-Job-ID": job.id,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.post(
                backend.endpoint.rstrip("/") + "/jobs",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not data.get("accepted", False):
                raise BackendError(f"{backend.name} rejected job")
            return data
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"{backend.name} dispatch failed: {exc}") from exc


backend_selector = BackendSelector()
