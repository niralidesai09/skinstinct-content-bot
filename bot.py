"""Local runner and admin commands for the Skinstinct content bot.

The pipeline itself lives in core.py. In production it runs on Vercel (app.py); this file is for running
it on your own machine with long polling, and for one-off admin tasks.

Usage:
  python bot.py                  run locally (long polling + Mon/Wed/Fri schedule). Only works while no webhook is set.
  python bot.py import PATH      import backlog notes (folder of .txt/.md files, or a Telegram Desktop result.json)
  python bot.py draft [NOTE_ID]  draft once and print it in the terminal
  python bot.py seed-voice       copy meera_voice_guide.txt into the voice_skill table
  python bot.py set-webhook URL  point Telegram at a deployed webhook (e.g. https://x.vercel.app/api/webhook)
  python bot.py delete-webhook   switch back to local polling

Storage is Supabase when SUPABASE_URL is set in .env, otherwise a local SQLite file (content.db).
"""

import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

import core
from core import MIN_SCORE, TZ, log, tg


def run():
    store = core.get_store()
    pipe = core.Pipeline(store)
    me = tg("getMe")
    info = tg("getWebhookInfo") or {}
    if info.get("url"):
        raise SystemExit(f"A webhook is set ({info['url']}), so Telegram won't deliver updates here. "
                         "Run `python bot.py delete-webhook` first to run locally.")
    log.info("Running locally as @%s. Review chat %s. Store: %s.", me["username"], core.review_chat(store),
             type(store).__name__)
    offset = int(store.kv_get("offset", 0))
    while True:
        try:
            slot = core.due_slot(dt.datetime.now(TZ))
            if slot and store.kv_get("last_slot") != slot:
                store.kv_set("last_slot", slot)
                log.info("Scheduled draft for slot %s", slot)
                pipe.scheduled_draft()
            updates = tg("getUpdates", offset=offset, timeout=50,
                         allowed_updates=["channel_post", "message", "callback_query"]) or []
            for upd in updates:
                offset = upd["update_id"] + 1
                store.kv_set("offset", offset)
                pipe.safe_handle(upd)
        except requests.RequestException as e:
            log.warning("Network error: %s. Retrying in 10s.", e)
            time.sleep(10)


def load_backlog(path):
    p = Path(path)
    if p.is_file() and p.suffix == ".json":  # Telegram Desktop: Export chat history -> JSON
        data = json.loads(p.read_text())
        for m in data.get("messages", []):
            t = m.get("text")
            if isinstance(t, list):
                t = "".join(x if isinstance(x, str) else x.get("text", "") for x in t)
            if m.get("type") == "message" and t and t.strip():
                yield t.strip()
    elif p.is_dir():
        for f in sorted(p.glob("*")):
            if f.suffix in (".txt", ".md") and f.read_text().strip():
                yield f.read_text().strip()
    else:
        raise SystemExit(f"Expected a folder of .txt/.md notes or a result.json export, got {path}")


def import_backlog(path):
    store = core.get_store()
    pipe = core.Pipeline(store)
    notes = list(load_backlog(path))
    print(f"Importing {len(notes)} notes and scoring each one...")
    passed = 0
    for text in notes:
        if store.note_text_exists(text):
            continue
        note_id = store.save_note(text, "import")
        t = pipe.triage_note(note_id)
        ok = t["score"] >= MIN_SCORE
        passed += ok
        print(f"#{note_id:>3}  {t['score']:>2}/10  {'pass' if ok else 'reject':<7} {t['category']:<22} {text[:60]!r}")
    print(f"Done. {passed} passed.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "google_genai", "google_genai.models", "google_genai.types"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    args = sys.argv[1:]
    if not args:
        run()
    elif args[0] == "import" and len(args) == 2:
        import_backlog(args[1])
    elif args[0] == "draft":
        store = core.get_store()
        pipe = core.Pipeline(store)
        note = store.get_note(int(args[1])) if len(args) > 1 else store.best_note()
        if not note:
            raise SystemExit("No note to draft from.")
        news = core.find_news(note["text"])
        d = core.write_draft(note, news, pipe.voice)
        used = bool(news) and d["USED NEWS"].lower().startswith("y")
        print(d["DRAFT"] + (f"\n\n{core.verify_block(news)}" if used else f"\n\n(news not used: {news})"))
        print(f"\n--- CHECK BEFORE POSTING\n{d['CHECK BEFORE POSTING']}")
    elif args[0] == "seed-voice":
        core.get_store().set_voice_skill(core.VOICE_FILE)
        print("Voice Skill saved to the voice_skill table.")
    elif args[0] == "set-webhook" and len(args) == 2:
        secret = os.environ.get("WEBHOOK_SECRET")
        if not secret:
            raise SystemExit("Set WEBHOOK_SECRET in .env (and in Vercel) first.")
        print(tg("setWebhook", url=args[1], secret_token=secret, drop_pending_updates=True,
                 allowed_updates=["channel_post", "message", "callback_query"]))
        print(tg("getWebhookInfo"))
    elif args[0] == "delete-webhook":
        print(tg("deleteWebhook"))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
