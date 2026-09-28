"""Render-compatible entry point.

Render may still use `python main.py`. This file intentionally delegates
startup to the real Pyrogram polling worker in bot_runner.py.
"""

import asyncio
import logging

from bot_runner import main as run_bot

log = logging.getLogger("vikky-main")

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Bot stopped by shutdown signal.")
    except Exception:
        log.exception("Bot failed during startup/runtime.")
        raise
