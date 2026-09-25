"""Skinstinct content bot.

Meera drops raw notes into her Telegram channel. This bot:
  1. saves every note and triages it (is it worth developing into a post?),
  2. on a schedule (default Mon/Wed/Fri 08:00 IST), or on /draft, picks the best
     undrafted note, finds one current news angle with web search, and drafts a
     LinkedIn post in Meera's voice,
  3. posts the draft back to Telegram for her to review - Approve / Redo / Skip.

It never publishes to LinkedIn. Meera reads, edits, and posts herself.

Usage:
  python bot.py                 run the bot (long polling + scheduler)
  python bot.py import PATH     import backlog notes (folder of .txt/.md files,
                                or a Telegram Desktop export result.json)
  python bot.py draft [NOTE_ID] draft once from the terminal and print it
"""

import warnings
warnings.filterwarnings("ignore")  # quiet library deprecation noise on Python 3.9

import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
NOTES_CHANNEL_ID = int(os.environ["NOTES_CHANNEL_ID"])
REVIEW_CHAT_ID = int(os.environ.get("REVIEW_CHAT_ID") or NOTES_CHANNEL_ID)
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))
DRAFT_DAYS = [d.strip().lower()[:3] for d in os.environ.get("DRAFT_DAYS", "mon,wed,fri").split(",")]
DRAFT_TIME = os.environ.get("DRAFT_TIME", "08:00")
MIN_SCORE = int(os.environ.get("MIN_SCORE", "6"))
TRIAGE_REPLIES = os.environ.get("TRIAGE_REPLIES", "true").lower() == "true"
DRAFT_MODEL = os.environ.get("GEMINI_DRAFT_MODEL", "gemini-pro-latest")
TRIAGE_MODEL = os.environ.get("GEMINI_TRIAGE_MODEL", "gemini-flash-latest")
DB_PATH = HERE / "content.db"

API = f"https://api.telegram.org/bot{TOKEN}"
TG_LIMIT = 4000

log = logging.getLogger("skinstinct")
gemini = genai.Client()  # reads GEMINI_API_KEY

VOICE_GUIDE = (HERE / "meera_voice_guide.txt").read_text()
PUBLISHED = (HERE / "published_linkedin.txt").read_text()

CATEGORIES = [
    "Ingredient Deep-Dive", "Formulation Science", "Industry Transparency",
    "India-Specific Context", "Brand Philosophy", "Consumer Education", "Founder Story",
]


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,              -- 'telegram' or 'import'
            tg_message_id INTEGER,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            score INTEGER,                     -- 1-10, set by triage
            verdict TEXT,                      -- develop / hold / discard
            category TEXT,
            angle TEXT,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'new', -- new / triaged / drafted / approved / skipped
            news TEXT                          -- suggested news angle, found at triage time
        );
        CREATE UNIQUE INDEX IF NOT EXISTS notes_tg ON notes(tg_message_id) WHERE tg_message_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL REFERENCES notes(id),
            text TEXT NOT NULL,
            news_angle TEXT,
            checks TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'  -- pending / approved / redone / skipped
        );
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
    """)
    if "news" not in [r["name"] for r in conn.execute("PRAGMA table_info(notes)")]:
        conn.execute("ALTER TABLE notes ADD COLUMN news TEXT")
    return conn


def kv_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(conn, k, v):
    conn.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    conn.commit()


def now_iso():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

http = requests.Session()


def tg(method, **params):
    """Call the Bot API, retrying network errors (flaky connections shouldn't lose a draft)."""
    read_timeout = params.get("timeout", 0) + 30
    for attempt in range(4):
        try:
            r = http.post(f"{API}/{method}", json=params, timeout=(10, read_timeout))
            break
        except requests.RequestException as e:
            if attempt == 3:
                raise
            log.warning("Telegram %s network error (%s), retry %d", method, type(e).__name__, attempt + 1)
            time.sleep(3 * (attempt + 1))
    data = r.json()
    if not data.get("ok"):
        log.error("Telegram %s failed: %s", method, data.get("description"))
    return data.get("result")


def send(chat_id, text, reply_to=None, buttons=None):
    """Send plain text, splitting at paragraph breaks if over Telegram's limit.
    Buttons go on the last chunk. Returns the last message sent."""
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > TG_LIMIT:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    chunks.append(cur)
    msg = None
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk[:4096], "disable_web_page_preview": True}
        if reply_to and i == 0:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if buttons and i == len(chunks) - 1:
            params["reply_markup"] = {"inline_keyboard": buttons}
        msg = tg("sendMessage", **params)
    return msg


def draft_buttons(draft_id):
    return [[
        {"text": "Approve", "callback_data": f"approve:{draft_id}"},
        {"text": "Redo", "callback_data": f"redo:{draft_id}"},
        {"text": "Reject", "callback_data": f"reject:{draft_id}"},
    ]]


# --------------------------------------------------------------------------
# Gemini calls
# --------------------------------------------------------------------------

def _generate(model, system, prompt, **config):
    """generate_content with retries on transient errors; returns the response."""
    for attempt in range(3):
        try:
            resp = gemini.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system, **config),
            )
        except genai.errors.APIError as e:
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            raise
        cand = (resp.candidates or [None])[0]
        if not cand or not _text(resp):
            reason = cand.finish_reason if cand else resp.prompt_feedback
            raise RuntimeError(f"Gemini returned no text ({reason}).")
        return resp


def _text(resp):
    parts = resp.candidates[0].content.parts or []
    return "".join(p.text for p in parts if p.text and not p.thought).strip()


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-10: how strong a LinkedIn post this note could become"},
        "category": {"type": "string", "enum": CATEGORIES},
        "angle": {"type": "string", "description": "One sentence: the post's core claim, in Meera's framing"},
        "reason": {"type": "string", "description": "One short sentence explaining the score"},
    },
    "required": ["score", "category", "angle", "reason"],
    "additionalProperties": False,
}

TRIAGE_SYSTEM = f"""You screen raw notes for Meera Pillai, founder of Skinstinct (Indian D2C skincare, ex-pharma formulation),
before anything is drafted. Her LinkedIn audience: 28-40 year old urban Indian women tired of being sold to, who respond
to founders who know their science. Her best post (niacinamide concentration vs label %) drove 340 profile visits and
3 wholesale enquiries.

Score 0-10 how strong a LinkedIn post this note could become, in HER voice. Notes scoring 6+ get drafted; below 6 do not.
- 8-10: a specific, first-hand observation (manufacturing, CoA, batch data, customer case, supplier conversation) that
  exposes a gap between a claim and formulation reality, with enough substance for 450-600 words.
- 6-7: a real, teachable point in her territory, but thinner or less first-hand.
- 4-5: an idea with no specific anchor yet, or something she says she has already covered with no new angle.
- 0-3: logistics, to-dos, reminders, abandoned half-sentences, personal notes, hype, anything that needs medical or
  dermatologist authority, or attacks on a named person or competitor.
Be strict. If every note passes, the screen is useless.

Categories: {", ".join(CATEGORIES)}.

Voice guide for reference:
{VOICE_GUIDE}"""


def triage(note_text):
    resp = _generate(
        TRIAGE_MODEL, TRIAGE_SYSTEM, f"Note:\n\n{note_text}",
        response_mime_type="application/json",
        response_json_schema=TRIAGE_SCHEMA,
    )
    return json.loads(_text(resp))


KEYWORDS_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "A 3-5 word news search phrase"}},
    "required": ["query"],
    "additionalProperties": False,
}


def news_query(note_text):
    resp = _generate(
        TRIAGE_MODEL,
        "Extract 3-5 search terms from this skincare founder's note and return one short news search phrase "
        "(3-5 words) likely to find a recent, relevant industry or regulatory news story. No brand names.",
        note_text,
        response_mime_type="application/json",
        response_json_schema=KEYWORDS_SCHEMA,
    )
    return json.loads(_text(resp))["query"]


def google_news(query):
    """Top Google News result for the query (free RSS, no key). Returns a dict or None."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    for q in (f"{query} when:90d", query, " ".join(query.split()[:2])):
        r = http.get("https://news.google.com/rss/search",
                     params={"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}, timeout=20)
        item = ET.fromstring(r.content).find("./channel/item")
        if item is None:
            continue
        source = item.findtext("source") or ""
        headline = item.findtext("title") or ""
        if source and headline.endswith(f" - {source}"):
            headline = headline[: -len(source) - 3]
        try:
            date = parsedate_to_datetime(item.findtext("pubDate")).strftime("%d %b %Y")
        except (TypeError, ValueError):
            date = item.findtext("pubDate") or ""
        return {"headline": headline, "source": source, "date": date, "link": item.findtext("link") or "", "query": q}
    return None


def find_news(note_text):
    return google_news(news_query(note_text))


def verify_block(news):
    line = "─" * 33
    return (f"{line}\nNEWS SOURCE: {news['headline']}\nFROM: {news['source']} · {news['date']}\nLINK: {news['link']}\n"
            f"⚠ Check this before publishing — you are the author of this claim\n{line}")


DRAFT_SYSTEM = f"""You draft LinkedIn posts for Meera Pillai, founder of Skinstinct. The draft goes to Meera for review;
she edits and publishes it herself. Your job is a draft she can publish with light edits, not one she has to rewrite.

Follow this voice guide exactly:

{VOICE_GUIDE}

Here are her four published LinkedIn posts. Match their structure, rhythm and restraint:

{PUBLISHED}

Hard rules:
- The post must be built on Meera's note. Her observation is the spine; do not replace it with a generic explainer.
- Facts: only use (a) what is in her note, (b) well-established formulation science, or (c) the news item provided.
  Never invent Skinstinct data (percentages, return rates, batch results, customer counts, timelines).
  If the post needs a number only Meera has, write [MEERA: <what's needed>] inline instead.
- Never state facts about Skinstinct (products it sells or is developing, timelines, costs, processes, results) unless
  they are in her note or her published posts. If a Skinstinct beat would help, use a [MEERA: ...] placeholder.
- News item: if it is genuinely relevant, use it to make the post timely. If it doesn't fit naturally, ignore it.
  Only claim what the headline itself says; do not add details you cannot see.
- Use her moves, not her sentences. Do not copy lines from the voice guide or published posts verbatim
  (e.g. "almost certainly meaningless", "I want to be precise about what I'm not saying", "the base isn't decoration").
- 450-600 words. Prose paragraphs. No headers, bullets, hashtags, emojis, exclamation marks, or sign-off.
  British spelling. Spaced hyphen " - " for dashes.
- Do not name or attack competitor brands or individuals.

Reply in exactly this format and nothing else:

DRAFT:
<the post>

USED NEWS:
<yes or no>

CHECK BEFORE POSTING:
<1-3 short lines: any [MEERA: ...] placeholders, any claim she should verify, anything you were unsure about>"""


def write_draft(note, news, redo_of=None):
    ask = (
        f"Meera's note (category: {note['category']}; core angle: {note['angle']}):\n\n"
        f"{note['text']}\n\nToday is {dt.date.today():%d %B %Y}.\n\n"
    )
    if news:
        ask += f"News item:\nHeadline: {news['headline']}\nSource: {news['source']}, {news['date']}"
    else:
        ask += "No news item was found. Write the post without one."
    if redo_of:
        ask += ("\n\nMeera asked for a different take than this earlier draft. Use a different opening and "
                f"structure:\n\n{redo_of}")
    return parse_draft(_text(_generate(DRAFT_MODEL, DRAFT_SYSTEM, ask)))


def parse_draft(raw):
    parts = {"DRAFT": "", "USED NEWS": "", "CHECK BEFORE POSTING": ""}
    key = None
    for line in raw.splitlines():
        head = line.strip().rstrip(":").upper()
        if head in parts:
            key = head
            continue
        if key:
            parts[key] += line + "\n"
    if not parts["DRAFT"].strip():
        parts["DRAFT"] = raw
    return {k: v.strip() for k, v in parts.items()}


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------

def save_note(conn, text, source, tg_message_id=None):
    cur = conn.execute(
        "INSERT OR IGNORE INTO notes(source, tg_message_id, text, created_at) VALUES(?, ?, ?, ?)",
        (source, tg_message_id, text, now_iso()),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def triage_note(conn, note_id):
    note = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    t = triage(note["text"])
    passed = t["score"] >= MIN_SCORE
    conn.execute(
        "UPDATE notes SET score=?, verdict=?, category=?, angle=?, reason=?, status=? WHERE id=?",
        (t["score"], "pass" if passed else "reject", t["category"], t["angle"], t["reason"],
         "triaged" if passed else "rejected", note_id),
    )
    conn.commit()
    return t


def best_note(conn):
    return conn.execute(
        "SELECT * FROM notes WHERE status='triaged' AND score>=? ORDER BY score DESC, created_at DESC LIMIT 1",
        (MIN_SCORE,),
    ).fetchone()


def make_and_send_draft(conn, note, chat_id=None, redo_of=None, reply_to=None):
    chat_id = chat_id or REVIEW_CHAT_ID
    news = json.loads(note["news"]) if note["news"] else None
    if note["news"] is None:
        news = find_news(note["text"])
        conn.execute("UPDATE notes SET news=? WHERE id=?", (json.dumps(news), note["id"]))
        conn.commit()
    d = write_draft(note, news, redo_of=redo_of)
    used_news = bool(news) and d["USED NEWS"].lower().startswith("y")
    body = d["DRAFT"] + (f"\n\n{verify_block(news)}" if used_news else "")
    cur = conn.execute(
        "INSERT INTO drafts(note_id, text, news_angle, checks, created_at) VALUES(?, ?, ?, ?, ?)",
        (note["id"], d["DRAFT"], json.dumps(news) if used_news else None, d["CHECK BEFORE POSTING"], now_iso()),
    )
    conn.execute("UPDATE notes SET status='drafted' WHERE id=?", (note["id"],))
    conn.commit()
    draft_id = cur.lastrowid
    header = f"DRAFT #{draft_id} - note #{note['id']} - {note['score']}/10 - {len(d['DRAFT'].split())} words"
    if news and not used_news:
        header += f"\n(News found but not a natural fit, so not used: {news['headline']} - {news['source']})"
    footer = (f"CHECK BEFORE POSTING\n{d['CHECK BEFORE POSTING'] or '-'}\n\n"
              f"Reply APPROVE or REJECT (or use the buttons). Status: pending.")
    send(chat_id, f"{header}\n\n{body}", reply_to=reply_to)
    send(chat_id, footer, buttons=draft_buttons(draft_id))
    return draft_id


def scheduled_draft(conn):
    note = best_note(conn)
    if not note:
        send(REVIEW_CHAT_ID, f"Scheduled draft: no waiting note scored {MIN_SCORE}/10 or higher, so nothing was drafted.")
        return
    make_and_send_draft(conn, note)


def due_slot(now):
    """Return a slot key like '2026-09-28 08:00' if a scheduled draft is due now."""
    if now.strftime("%a").lower()[:3] not in DRAFT_DAYS:
        return None
    hh, mm = map(int, DRAFT_TIME.split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < slot or now - slot > dt.timedelta(hours=3):
        return None
    return slot.strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

HELP = (
    "How this works\n\n"
    f"Send me a note. I score it 0-10. Below {MIN_SCORE}, I tell you why and stop. "
    f"{MIN_SCORE} or above, I find a current news story on Google News and draft a LinkedIn post in your voice. "
    "Nothing is published anywhere - you approve or reject every draft.\n\n"
    "APPROVE / REJECT - update the latest pending draft (or APPROVE 3 for draft #3)\n"
    "/draft 12 - draft note #12\n"
    "/queue - scored notes waiting for a draft\n"
    "/help - this message"
)


def handle_command(conn, text, chat_id):
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/draft":
        if len(parts) > 1 and parts[1].lstrip("#").isdigit():
            note = conn.execute("SELECT * FROM notes WHERE id=?", (int(parts[1].lstrip("#")),)).fetchone()
            if not note:
                send(chat_id, f"No note #{parts[1]}.")
                return
        else:
            note = best_note(conn)
            if not note:
                send(chat_id, f"No waiting note scored {MIN_SCORE}/10 or higher.")
                return
        send(chat_id, f"Drafting note #{note['id']}...")
        make_and_send_draft(conn, note, chat_id)
    elif cmd == "/queue":
        rows = conn.execute("SELECT * FROM notes WHERE status='triaged' ORDER BY score DESC, created_at DESC LIMIT 8").fetchall()
        if not rows:
            send(chat_id, "Queue is empty.")
            return
        lines = [f"#{r['id']} - {r['score']}/10 - {r['category']}\n{r['angle']}" for r in rows]
        send(chat_id, "Waiting notes, best first\n\n" + "\n\n".join(lines))
    else:
        send(chat_id, HELP)


def set_draft_status(conn, draft, status, chat_id):
    conn.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft["id"]))
    conn.execute("UPDATE notes SET status=? WHERE id=?", ("approved" if status == "approved" else "draft_rejected", draft["note_id"]))
    conn.commit()
    if status == "approved":
        send(chat_id, f"Draft #{draft['id']} status: APPROVED. Copy it into LinkedIn when you're ready.")
    else:
        send(chat_id, f"Draft #{draft['id']} status: REJECTED. It's kept on record, not deleted.")


def handle_decision(conn, text, chat_id):
    """Typed APPROVE / REJECT, optionally with a draft number."""
    parts = text.upper().replace("#", " ").split()
    status = "approved" if parts[0] == "APPROVE" else "rejected"
    if len(parts) > 1 and parts[1].isdigit():
        draft = conn.execute("SELECT * FROM drafts WHERE id=? AND status='pending'", (int(parts[1]),)).fetchone()
    else:
        draft = conn.execute("SELECT * FROM drafts WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
    if not draft:
        send(chat_id, "No pending draft to update.")
        return
    set_draft_status(conn, draft, status, chat_id)


def handle_note(conn, msg):
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat_id = msg["chat"]["id"]
    if not text:
        return
    if text.startswith("/"):
        handle_command(conn, text, chat_id)
        return
    if text.split()[0].upper().rstrip(".!") in ("APPROVE", "REJECT") and len(text.split()) <= 2:
        handle_decision(conn, text, chat_id)
        return
    note_id = save_note(conn, text, "telegram", msg["message_id"])
    if not note_id:
        return
    t = triage_note(conn, note_id)
    log.info("Note #%s scored %s/10", note_id, t["score"])
    if t["score"] < MIN_SCORE:
        send(chat_id, f"Note #{note_id}\nScore: {t['score']}/10 - NO DRAFT\nWhy: {t['reason']}", reply_to=msg["message_id"])
        return
    send(chat_id, f"Note #{note_id}\nScore: {t['score']}/10 - PASS\nWhy: {t['reason']}\n\n"
                  "Finding a news angle and drafting. About a minute.", reply_to=msg["message_id"])
    note = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    make_and_send_draft(conn, note, chat_id, reply_to=msg["message_id"])


def handle_callback(conn, cq):
    action, draft_id = cq["data"].split(":")
    draft = conn.execute("SELECT * FROM drafts WHERE id=?", (int(draft_id),)).fetchone()
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if not draft or draft["status"] != "pending":
        return
    msg = cq["message"]
    chat_id = msg["chat"]["id"]
    tg("editMessageReplyMarkup", chat_id=chat_id, message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
    if action == "approve":
        set_draft_status(conn, draft, "approved", chat_id)
    elif action == "reject":
        set_draft_status(conn, draft, "rejected", chat_id)
    elif action == "redo":
        conn.execute("UPDATE drafts SET status='redone' WHERE id=?", (draft["id"],))
        conn.commit()
        note = conn.execute("SELECT * FROM notes WHERE id=?", (draft["note_id"],)).fetchone()
        send(chat_id, f"Redrafting note #{note['id']}...")
        make_and_send_draft(conn, note, chat_id, redo_of=draft["text"])


def claim_review_chat(conn, chat_id):
    """The first person to message the bot privately becomes the reviewer: drafts go to that chat."""
    global REVIEW_CHAT_ID
    REVIEW_CHAT_ID = chat_id
    kv_set(conn, "review_chat", chat_id)
    log.info("Review chat set to %s", chat_id)
    send(chat_id, "Connected. Send me a note here (or drop it in the channel) and I'll score it and draft a post.\n\n" + HELP)


def handle_update(conn, upd):
    if "channel_post" in upd or "message" in upd:
        msg = upd.get("channel_post") or upd.get("message")
        chat_id, text = msg["chat"]["id"], msg.get("text") or ""
        if chat_id == NOTES_CHANNEL_ID:
            handle_note(conn, msg)
        elif msg["chat"].get("type") == "private" and not kv_get(conn, "review_chat") and not os.environ.get("REVIEW_CHAT_ID"):
            claim_review_chat(conn, chat_id)
        elif chat_id == REVIEW_CHAT_ID:
            handle_note(conn, msg)
        elif msg["chat"].get("type") == "private":
            send(chat_id, "This is a private bot.")
    elif "callback_query" in upd:
        cq = upd["callback_query"]
        if cq.get("message", {}).get("chat", {}).get("id") in (REVIEW_CHAT_ID, NOTES_CHANNEL_ID):
            handle_callback(conn, cq)


def run():
    global REVIEW_CHAT_ID
    conn = db()
    if not os.environ.get("REVIEW_CHAT_ID") and kv_get(conn, "review_chat"):
        REVIEW_CHAT_ID = int(kv_get(conn, "review_chat"))
    me = tg("getMe")
    log.info("Running as @%s. Notes channel %s, review chat %s, drafts %s at %s %s.",
             me["username"], NOTES_CHANNEL_ID, REVIEW_CHAT_ID, ",".join(DRAFT_DAYS), DRAFT_TIME, TZ)
    offset = int(kv_get(conn, "offset", 0))
    while True:
        try:
            slot = due_slot(dt.datetime.now(TZ))
            if slot and kv_get(conn, "last_slot") != slot:
                kv_set(conn, "last_slot", slot)
                log.info("Scheduled draft for slot %s", slot)
                scheduled_draft(conn)

            updates = tg("getUpdates", offset=offset, timeout=50,
                         allowed_updates=["channel_post", "message", "callback_query"]) or []
            for upd in updates:
                offset = upd["update_id"] + 1
                kv_set(conn, "offset", offset)
                try:
                    handle_update(conn, upd)
                except Exception as e:  # keep the bot alive; tell Meera what failed
                    log.exception("Update %s failed", upd["update_id"])
                    reason = "Telegram connection dropped" if isinstance(e, requests.RequestException) else str(e)[:200]
                    try:
                        send(REVIEW_CHAT_ID, f"Something went wrong ({reason}). Nothing was lost - try again, or /queue to see notes.")
                    except requests.RequestException:
                        pass
        except requests.RequestException as e:
            log.warning("Network error: %s. Retrying in 10s.", e)
            time.sleep(10)


# --------------------------------------------------------------------------
# Backlog import
# --------------------------------------------------------------------------

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
    conn = db()
    notes = list(load_backlog(path))
    print(f"Importing {len(notes)} notes and triaging each one...")
    for text in notes:
        if conn.execute("SELECT 1 FROM notes WHERE text=?", (text,)).fetchone():
            continue
        note_id = save_note(conn, text, "import")
        t = triage_note(conn, note_id)
        verdict = "pass" if t["score"] >= MIN_SCORE else "reject"
        print(f"#{note_id:>3}  {t['score']:>2}/10  {verdict:<7} {t['category']:<22} {text[:60]!r}")
    counts = conn.execute("SELECT verdict, COUNT(*) n FROM notes GROUP BY verdict").fetchall()
    print("Done. " + ", ".join(f"{r['verdict']}: {r['n']}" for r in counts))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "google_genai", "google_genai.models", "google_genai.types"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    args = sys.argv[1:]
    if args[:1] == ["import"] and len(args) == 2:
        import_backlog(args[1])
    elif args[:1] == ["draft"]:
        conn = db()
        note = (conn.execute("SELECT * FROM notes WHERE id=?", (int(args[1]),)).fetchone()
                if len(args) > 1 else best_note(conn))
        if not note:
            raise SystemExit("No note to draft from.")
        news = find_news(note["text"])
        d = write_draft(note, news)
        used = bool(news) and d["USED NEWS"].lower().startswith("y")
        print(d["DRAFT"] + (f"\n\n{verify_block(news)}" if used else f"\n\n(news not used: {news})"))
        print(f"\n--- CHECK BEFORE POSTING\n{d['CHECK BEFORE POSTING']}")
    elif not args:
        run()
    else:
        print(__doc__)
