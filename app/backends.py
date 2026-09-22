from dataclasses import dataclass

from .config import settings
from .models import Job, JobType


@dataclass(frozen=True, slots=True)
class Backend:
    name: str
    enabled: bool
    supports_gpu: bool
    supports_cpu: bool


class BackendSelector:
    """Selects only configured backends; priority is Lightning -> Modal -> Kaggle.

    Actual provider API adapters are intentionally separate from selection logic.
    """

    def __init__(self) -> None:
        self.backends = (
            Backend("lightning", settings.lightning_enabled, True, True),
            Backend("modal", settings.modal_enabled, True, True),
            Backend("kaggle", settings.kaggle_enabled, True, True),
        )

    def select(self, job: Job) -> Backend | None:
        gpu_required = job.type is JobType.UPSCALE
        for backend in self.backends:
            if not backend.enabled:
                continue
            if gpu_required and backend.supports_gpu:
                return backend
            if not gpu_required and backend.supports_cpu:
                return backend
        return None


backend_selector = BackendSelector()
