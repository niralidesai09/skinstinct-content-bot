"""Vercel cron: drafts from the best waiting note on Mon/Wed/Fri (schedule in vercel.json, times in UTC)."""

import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Vercel sends "Authorization: Bearer <CRON_SECRET>" on cron calls when CRON_SECRET is set.
        expected = f"Bearer {os.environ.get('CRON_SECRET', '')}"
        if not os.environ.get("CRON_SECRET") or not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            self.send_response(401)
            self.end_headers()
            return
        core.Pipeline(core.get_store()).scheduled_draft()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
