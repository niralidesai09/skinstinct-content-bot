"""Vercel entrypoint (a plain WSGI app, no framework).

  POST /api/webhook  Telegram delivers every bot update here (set with `python bot.py set-webhook URL`)
  GET  /api/cron     Vercel cron: drafts from the best waiting note on Mon/Wed/Fri (schedule in vercel.json, UTC)
  GET  /             health check
"""

import hmac
import json
import os

import core


def _respond(start_response, status, body):
    start_response(status, [("Content-Type", "text/plain; charset=utf-8")])
    return [body.encode()]


def _secret_ok(given, env_name):
    expected = os.environ.get(env_name, "")
    return bool(expected) and hmac.compare_digest(given, expected)


def webhook(environ, start_response):
    raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
    if not _secret_ok(environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", ""), "WEBHOOK_SECRET"):
        return _respond(start_response, "401 Unauthorized", "unauthorized")
    try:
        update = json.loads(raw or b"{}")
    except ValueError:
        return _respond(start_response, "400 Bad Request", "bad json")
    store = core.get_store()
    # Drafting takes about a minute; if Telegram re-sends the same update meanwhile, handle it only once.
    if "update_id" in update and not store.claim_update(update["update_id"]):
        return _respond(start_response, "200 OK", "duplicate")
    core.Pipeline(store).safe_handle(update)
    return _respond(start_response, "200 OK", "ok")


def cron(environ, start_response):
    # Vercel sends "Authorization: Bearer <CRON_SECRET>" on cron calls when CRON_SECRET is set.
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if not _secret_ok(auth[len("Bearer "):] if auth.startswith("Bearer ") else "", "CRON_SECRET"):
        return _respond(start_response, "401 Unauthorized", "unauthorized")
    core.Pipeline(core.get_store()).scheduled_draft()
    return _respond(start_response, "200 OK", "ok")


def app(environ, start_response):
    path, method = environ.get("PATH_INFO", "/").rstrip("/") or "/", environ.get("REQUEST_METHOD", "GET")
    if path == "/api/webhook" and method == "POST":
        return webhook(environ, start_response)
    if path == "/api/cron" and method == "GET":
        return cron(environ, start_response)
    if path in ("/", "/api/webhook"):
        return _respond(start_response, "200 OK", "Skinstinct content bot is running. Telegram posts updates to /api/webhook.")
    return _respond(start_response, "404 Not Found", "not found")
