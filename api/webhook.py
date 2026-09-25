"""Vercel function: Telegram delivers every bot update here (set with `python bot.py set-webhook URL`)."""

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _reply(self, code, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, b"Skinstinct webhook is running. Telegram sends updates here with POST.")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        secret = os.environ.get("WEBHOOK_SECRET", "")
        given = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(given, secret):
            return self._reply(401, b"unauthorized")
        try:
            update = json.loads(raw or b"{}")
        except ValueError:
            return self._reply(400, b"bad json")
        store = core.get_store()
        # Drafting takes about a minute; if Telegram re-sends the same update meanwhile, handle it only once.
        if "update_id" in update and not store.claim_update(update["update_id"]):
            return self._reply(200)
        core.Pipeline(store).safe_handle(update)
        self._reply(200)
