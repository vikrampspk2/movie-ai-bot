from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    telegram_bot_token: str = ""
    bot_admin_id: int | None = None
    webhook_secret: str = ""
    workspace_root: Path = Path("./workspace")
    max_workspace_gb: float = 50.0
    default_queue_concurrency: int = 1
    lightning_enabled: bool = False
    modal_enabled: bool = False
    kaggle_enabled: bool = False
    telegram_max_upload_retries: int = 3
    external_upload_retries: int = 3
    gpu_required_for_upscale: bool = True
    ffmpeg_threads: int = 0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
