# Vikky Movie AI Bot

Production-oriented foundation for a Telegram media processing bot.

## Architecture

Telegram Bot → Queue/Job Manager → Backend Selector → Processing → Verification → Telegram Output

Backend priority is designed as **Lightning AI → Modal → Kaggle**. Provider adapters will be added separately and only enabled through environment configuration.

## Current foundation

- Telegram `/start`, `/help`, `/status`, `/queue`, `/cancel`
- Typed job model for SYNC / AI UPSCALE / ENCODE
- Async in-process queue foundation
- Backend selection layer
- FastAPI `/health` endpoint
- Environment-based secrets; no credentials are committed

## Planned verified processing stages

1. Safe media/ZIP/ISO detection and extraction
2. MediaInfo + ffprobe inspection
3. CPU-based precision audio/video synchronization
4. Real AI 4K upscaling with GPU selection
5. MKV encoding with NVENC/CPU fallback
6. Output verification and Telegram upload
7. Checkpoints, retry/failover and cleanup after successful upload

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

Never commit `.env` or real credentials.
