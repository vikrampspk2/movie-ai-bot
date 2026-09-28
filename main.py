"""Render entry point for the real Telegram polling worker."""

import asyncio
import logging

from bot_runner import main as run_bot

log = logging.getLogger("vikky-main")

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Bot shutdown signal vachindi.")
    except Exception:
        log.exception("Bot startup/runtime lo ibbandi vachindi.")
        raise
