"""Render entry point for the Telegram polling worker and Web Service health port."""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot_runner import main as run_bot

log = logging.getLogger("vikky-main")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_render_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="render-health",
        daemon=True,
    )
    thread.start()
    log.info("Render health server listening on port %s", port)
    return server


if __name__ == "__main__":
    health_server = start_render_health_server()
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Bot shutdown signal vachindi.")
    except Exception:
        log.exception("Bot startup/runtime lo ibbandi vachindi.")
        raise
    finally:
        health_server.shutdown()
        health_server.server_close()
