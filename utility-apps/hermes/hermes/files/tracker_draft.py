"""Model-only bridge for private Interviews Tracker drafts."""
import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore

from agent.auxiliary_client import resolve_provider_client

logging.disable(logging.CRITICAL)
MODEL = "gpt-6.1-sol"
SLOTS = BoundedSemaphore(2)
TOKEN = os.environ["TRACKER_DRAFT_TOKEN"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200, {"status": "ok"}) if self.path == "/health" else self.reply(404, {})

    def do_POST(self):
        if self.path != "/draft":
            return self.reply(404, {})
        if not TOKEN or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN):
            return self.reply(401, {})
        self.connection.settimeout(5)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 120_000:
                return self.reply(413, {})
            data = json.loads(self.rfile.read(length))
            system, context = data["system"], data["context"]
            if not (isinstance(system, str) and 0 < len(system) <= 12000 and
                    isinstance(context, str) and 0 < len(context) <= 50000):
                return self.reply(422, {})
        except (ValueError, TypeError, KeyError, OSError):
            return self.reply(400, {})
        if not SLOTS.acquire(blocking=False):
            return self.reply(429, {})
        try:
            client, model = resolve_provider_client("openai-codex", MODEL)
            if client is None:
                return self.reply(503, {})
            response = client.chat.completions.create(model=model, messages=[
                {"role": "system", "content": system}, {"role": "user", "content": context},
            ], tools=[], max_tokens=1500, timeout=30)
            text = response.choices[0].message.content
            if not isinstance(text, str) or not text.strip():
                return self.reply(502, {})
        except Exception:
            return self.reply(503, {})
        finally:
            SLOTS.release()
        self.reply(200, {"text": text.strip(), "model": model})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8643), Handler).serve_forever()
