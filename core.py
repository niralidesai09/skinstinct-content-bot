"""Skinstinct content pipeline - shared by the local runner (bot.py) and Vercel (api/).

note -> score (Gemini Flash) -> below MIN_SCORE: explain and stop
                              -> otherwise: keywords (Flash) -> Google News -> draft (Gemini Pro, Voice Skill)
     -> draft back to Telegram with a verify block -> Meera replies APPROVE / REJECT

Nothing is ever published to LinkedIn.
"""

import warnings
warnings.filterwarnings("ignore")  # quiet library deprecation noise

import datetime as dt
import json
import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types

HERE = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
NOTES_CHANNEL_ID = int(os.environ["NOTES_CHANNEL_ID"])
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))
DRAFT_DAYS = [d.strip().lower()[:3] for d in os.environ.get("DRAFT_DAYS", "mon,wed,fri").split(",")]
DRAFT_TIME = os.environ.get("DRAFT_TIME", "08:00")
MIN_SCORE = int(os.environ.get("MIN_SCORE", "6"))
DRAFT_MODEL = os.environ.get("GEMINI_DRAFT_MODEL", "gemini-pro-latest")
TRIAGE_MODEL = os.environ.get("GEMINI_TRIAGE_MODEL", "gemini-flash-latest")

API = f"https://api.telegram.org/bot{TOKEN}"
TG_LIMIT = 4000

log = logging.getLogger("skinstinct")
gemini = genai.Client()  # reads GEMINI_API_KEY
http = requests.Session()

VOICE_FILE = (HERE / "meera_voice_guide.txt").read_text()
PUBLISHED = (HERE / "published_linkedin.txt").read_text()

CATEGORIES = [
    "Ingredient Deep-Dive", "Formulation Science", "Industry Transparency",
    "India-Specific Context", "Brand Philosophy", "Consumer Education", "Founder Story",
]


def now_iso():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Storage: SQLite locally, Supabase when SUPABASE_URL is set
# --------------------------------------------------------------------------

class SqliteStore:
    def __init__(self, path=HERE / "content.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                chat_id INTEGER,
                tg_message_id INTEGER,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                score INTEGER, verdict TEXT, category TEXT, angle TEXT, reason TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                news TEXT,
                UNIQUE(chat_id, tg_message_id)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id INTEGER NOT NULL REFERENCES notes(id),
                text TEXT NOT NULL, news_angle TEXT, checks TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS voice_skill (name TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS processed_updates (update_id INTEGER PRIMARY KEY);
        """)

    def _one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def save_note(self, text, source, chat_id=None, tg_message_id=None):
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO notes(source, chat_id, tg_message_id, text, created_at) VALUES(?, ?, ?, ?, ?)",
            (source, chat_id, tg_message_id, text, now_iso()))
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def get_note(self, note_id):
        return self._one("SELECT * FROM notes WHERE id=?", (note_id,))

    def update_note(self, note_id, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE notes SET {sets} WHERE id=?", (*fields.values(), note_id))
        self.conn.commit()

    def best_note(self):
        return self._one("SELECT * FROM notes WHERE status='triaged' AND score>=? "
                         "ORDER BY score DESC, created_at DESC LIMIT 1", (MIN_SCORE,))

    def queue(self, limit=8):
        rows = self.conn.execute("SELECT * FROM notes WHERE status='triaged' "
                                 "ORDER BY score DESC, created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def note_text_exists(self, text):
        return self._one("SELECT id FROM notes WHERE text=?", (text,)) is not None

    def insert_draft(self, note_id, text, news_angle, checks):
        cur = self.conn.execute(
            "INSERT INTO drafts(note_id, text, news_angle, checks, created_at) VALUES(?, ?, ?, ?, ?)",
            (note_id, text, news_angle, checks, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def get_draft(self, draft_id):
        return self._one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def latest_pending_draft(self):
        return self._one("SELECT * FROM drafts WHERE status='pending' ORDER BY id DESC LIMIT 1")

    def update_draft(self, draft_id, status):
        self.conn.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
        self.conn.commit()

    def voice_skill(self):
        row = self._one("SELECT content FROM voice_skill WHERE name='meera'")
        return row["content"] if row else None

    def set_voice_skill(self, content):
        self.conn.execute("INSERT INTO voice_skill(name, content, updated_at) VALUES('meera', ?, ?) "
                          "ON CONFLICT(name) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                          (content, now_iso()))
        self.conn.commit()

    def kv_get(self, k, default=None):
        row = self._one("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else default

    def kv_set(self, k, v):
        self.conn.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        self.conn.commit()

    def claim_update(self, update_id):
        cur = self.conn.execute("INSERT OR IGNORE INTO processed_updates(update_id) VALUES(?)", (update_id,))
        self.conn.commit()
        return cur.rowcount == 1


class SupabaseStore:
    """Same interface over Supabase's REST API (service role key; tables have RLS on, no policies)."""

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _req(self, method, table, params=None, body=None, prefer="return=representation"):
        r = http.request(method, f"{self.base}/{table}", params=params, json=body,
                         headers={**self.h, "Prefer": prefer}, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {method} {table} failed: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else []

    def _first(self, table, params):
        rows = self._req("GET", table, {**params, "limit": 1})
        return rows[0] if rows else None

    def save_note(self, text, source, chat_id=None, tg_message_id=None):
        rows = self._req("POST", "notes", {"on_conflict": "chat_id,tg_message_id"},
                         {"source": source, "chat_id": chat_id, "tg_message_id": tg_message_id, "text": text},
                         prefer="return=representation,resolution=ignore-duplicates")
        return rows[0]["id"] if rows else None

    def get_note(self, note_id):
        return self._first("notes", {"id": f"eq.{note_id}"})

    def update_note(self, note_id, **fields):
        self._req("PATCH", "notes", {"id": f"eq.{note_id}"}, fields, prefer="return=minimal")

    def best_note(self):
        return self._first("notes", {"status": "eq.triaged", "score": f"gte.{MIN_SCORE}",
                                     "order": "score.desc,created_at.desc"})

    def queue(self, limit=8):
        return self._req("GET", "notes", {"status": "eq.triaged", "order": "score.desc,created_at.desc", "limit": limit})

    def note_text_exists(self, text):
        return self._first("notes", {"text": f"eq.{text}", "select": "id"}) is not None

    def insert_draft(self, note_id, text, news_angle, checks):
        rows = self._req("POST", "drafts", body={"note_id": note_id, "text": text, "news_angle": news_angle, "checks": checks})
        return rows[0]["id"]

    def get_draft(self, draft_id):
        return self._first("drafts", {"id": f"eq.{draft_id}"})

    def latest_pending_draft(self):
        return self._first("drafts", {"status": "eq.pending", "order": "id.desc"})

    def update_draft(self, draft_id, status):
        self._req("PATCH", "drafts", {"id": f"eq.{draft_id}"}, {"status": status}, prefer="return=minimal")

    def voice_skill(self):
        row = self._first("voice_skill", {"name": "eq.meera", "select": "content"})
        return row["content"] if row else None

    def set_voice_skill(self, content):
        self._req("POST", "voice_skill", {"on_conflict": "name"},
                  {"name": "meera", "content": content, "updated_at": now_iso()},
                  prefer="return=minimal,resolution=merge-duplicates")

    def kv_get(self, k, default=None):
        row = self._first("kv", {"k": f"eq.{k}"})
        return row["v"] if row else default

    def kv_set(self, k, v):
        self._req("POST", "kv", {"on_conflict": "k"}, {"k": k, "v": str(v)},
                  prefer="return=minimal,resolution=merge-duplicates")

    def claim_update(self, update_id):
        rows = self._req("POST", "processed_updates", {"on_conflict": "update_id"}, {"update_id": update_id},
                         prefer="return=representation,resolution=ignore-duplicates")
        return bool(rows)


def get_store():
    if os.environ.get("SUPABASE_URL"):
        return SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    return SqliteStore()


def review_chat(store):
    """Where drafts go: REVIEW_CHAT_ID, else the private chat that claimed the bot, else the notes channel."""
    return int(os.environ.get("REVIEW_CHAT_ID") or store.kv_get("review_chat") or NOTES_CHANNEL_ID)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

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
    """Send plain text, splitting at paragraph breaks if over Telegram's limit. Buttons go on the last chunk."""
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
# Gemini
# --------------------------------------------------------------------------

def _generate(model, system, prompt, **config):
    """generate_content with retries on transient errors."""
    for attempt in range(3):
        try:
            resp = gemini.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system, **config))
        except genai.errors.APIError as e:
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        cand = (resp.candidates or [None])[0]
        if not cand or not _text(resp):
            raise RuntimeError(f"Gemini returned no text ({cand.finish_reason if cand else resp.prompt_feedback}).")
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


def triage_system(voice):
    return f"""You screen raw notes for Meera Pillai, founder of Skinstinct (Indian D2C skincare, ex-pharma formulation),
before anything is drafted. Her LinkedIn audience: 28-40 year old urban Indian women tired of being sold to, who respond
to founders who know their science. Her best post (niacinamide concentration vs label %) drove 340 profile visits and
3 wholesale enquiries.

Score 0-10 how strong a LinkedIn post this note could become, in HER voice. Notes scoring {MIN_SCORE}+ get drafted; below do not.
- 8-10: a specific, first-hand observation (manufacturing, CoA, batch data, customer case, supplier conversation) that
  exposes a gap between a claim and formulation reality, with enough substance for 450-600 words.
- 6-7: a real, teachable point in her territory, but thinner or less first-hand.
- 4-5: an idea with no specific anchor yet, or something she says she has already covered with no new angle.
- 0-3: logistics, to-dos, reminders, abandoned half-sentences, personal notes, hype, anything that needs medical or
  dermatologist authority, or attacks on a named person or competitor.
Be strict. If every note passes, the screen is useless.

Categories: {", ".join(CATEGORIES)}.

Voice guide for reference:
{voice}"""


def triage(note_text, voice):
    resp = _generate(TRIAGE_MODEL, triage_system(voice), f"Note:\n\n{note_text}",
                     response_mime_type="application/json", response_json_schema=TRIAGE_SCHEMA)
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
        note_text, response_mime_type="application/json", response_json_schema=KEYWORDS_SCHEMA)
    return json.loads(_text(resp))["query"]


def google_news(query):
    """Top Google News result for the query (free RSS, no key). Returns a dict or None."""
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


def draft_system(voice):
    return f"""You draft LinkedIn posts for Meera Pillai, founder of Skinstinct. The draft goes to Meera for review;
she edits and publishes it herself. Your job is a draft she can publish with light edits, not one she has to rewrite.

Follow this voice guide exactly:

{voice}

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


def write_draft(note, news, voice, redo_of=None):
    ask = (f"Meera's note (category: {note['category']}; core angle: {note['angle']}):\n\n"
           f"{note['text']}\n\nToday is {dt.date.today():%d %B %Y}.\n\n")
    if news:
        ask += f"News item:\nHeadline: {news['headline']}\nSource: {news['source']}, {news['date']}"
    else:
        ask += "No news item was found. Write the post without one."
    if redo_of:
        ask += ("\n\nMeera asked for a different take than this earlier draft. Use a different opening and "
                f"structure:\n\n{redo_of}")
    return parse_draft(_text(_generate(DRAFT_MODEL, draft_system(voice), ask)))


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

class Pipeline:
    def __init__(self, store):
        self.store = store
        self.voice = store.voice_skill() or VOICE_FILE  # Voice Skill from the database, file as fallback

    def triage_note(self, note_id):
        note = self.store.get_note(note_id)
        t = triage(note["text"], self.voice)
        passed = t["score"] >= MIN_SCORE
        self.store.update_note(note_id, score=t["score"], verdict="pass" if passed else "reject",
                               category=t["category"], angle=t["angle"], reason=t["reason"],
                               status="triaged" if passed else "rejected")
        return t

    def make_and_send_draft(self, note, chat_id=None, redo_of=None, reply_to=None):
        chat_id = chat_id or review_chat(self.store)
        news = json.loads(note["news"]) if note.get("news") else None
        if note.get("news") is None:
            news = find_news(note["text"])
            self.store.update_note(note["id"], news=json.dumps(news))
        d = write_draft(note, news, self.voice, redo_of=redo_of)
        used_news = bool(news) and d["USED NEWS"].lower().startswith("y")
        body = d["DRAFT"] + (f"\n\n{verify_block(news)}" if used_news else "")
        draft_id = self.store.insert_draft(note["id"], d["DRAFT"], json.dumps(news) if used_news else None,
                                           d["CHECK BEFORE POSTING"])
        self.store.update_note(note["id"], status="drafted")
        header = f"DRAFT #{draft_id} - note #{note['id']} - {note['score']}/10 - {len(d['DRAFT'].split())} words"
        if news and not used_news:
            header += f"\n(News found but not a natural fit, so not used: {news['headline']} - {news['source']})"
        footer = (f"CHECK BEFORE POSTING\n{d['CHECK BEFORE POSTING'] or '-'}\n\n"
                  "Reply APPROVE or REJECT (or use the buttons). Status: pending.")
        send(chat_id, f"{header}\n\n{body}", reply_to=reply_to)
        send(chat_id, footer, buttons=draft_buttons(draft_id))
        return draft_id

    def scheduled_draft(self):
        note = self.store.best_note()
        if not note:
            send(review_chat(self.store),
                 f"Scheduled draft: no waiting note scored {MIN_SCORE}/10 or higher, so nothing was drafted.")
            return
        self.make_and_send_draft(note)

    # ---- handlers ----

    def handle_command(self, text, chat_id):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/draft":
            if len(parts) > 1 and parts[1].lstrip("#").isdigit():
                note = self.store.get_note(int(parts[1].lstrip("#")))
                if not note:
                    send(chat_id, f"No note #{parts[1]}.")
                    return
                if note["score"] is None:
                    self.triage_note(note["id"])
                    note = self.store.get_note(note["id"])
            else:
                note = self.store.best_note()
                if not note:
                    send(chat_id, f"No waiting note scored {MIN_SCORE}/10 or higher.")
                    return
            send(chat_id, f"Drafting note #{note['id']}...")
            self.make_and_send_draft(note, chat_id)
        elif cmd == "/queue":
            rows = self.store.queue()
            if not rows:
                send(chat_id, "Queue is empty.")
                return
            lines = [f"#{r['id']} - {r['score']}/10 - {r['category']}\n{r['angle']}" for r in rows]
            send(chat_id, "Waiting notes, best first\n\n" + "\n\n".join(lines))
        else:
            send(chat_id, HELP)

    def set_draft_status(self, draft, status, chat_id):
        self.store.update_draft(draft["id"], status)
        self.store.update_note(draft["note_id"], status="approved" if status == "approved" else "draft_rejected")
        if status == "approved":
            send(chat_id, f"Draft #{draft['id']} status: APPROVED. Copy it into LinkedIn when you're ready.")
        else:
            send(chat_id, f"Draft #{draft['id']} status: REJECTED. It's kept on record, not deleted.")

    def handle_decision(self, text, chat_id):
        parts = text.upper().replace("#", " ").split()
        status = "approved" if parts[0].startswith("APPROVE") else "rejected"
        if len(parts) > 1 and parts[1].isdigit():
            draft = self.store.get_draft(int(parts[1]))
            draft = draft if draft and draft["status"] == "pending" else None
        else:
            draft = self.store.latest_pending_draft()
        if not draft:
            send(chat_id, "No pending draft to update.")
            return
        self.set_draft_status(draft, status, chat_id)

    def handle_note(self, msg):
        text = (msg.get("text") or msg.get("caption") or "").strip()
        chat_id = msg["chat"]["id"]
        if not text:
            return
        if text.startswith("/"):
            self.handle_command(text, chat_id)
            return
        words = text.split()
        if words[0].upper().rstrip(".!") in ("APPROVE", "REJECT") and len(words) <= 2:
            self.handle_decision(text, chat_id)
            return
        note_id = self.store.save_note(text, "telegram", chat_id, msg["message_id"])
        if not note_id:
            return
        t = self.triage_note(note_id)
        log.info("Note #%s scored %s/10", note_id, t["score"])
        if t["score"] < MIN_SCORE:
            send(chat_id, f"Note #{note_id}\nScore: {t['score']}/10 - NO DRAFT\nWhy: {t['reason']}",
                 reply_to=msg["message_id"])
            return
        send(chat_id, f"Note #{note_id}\nScore: {t['score']}/10 - PASS\nWhy: {t['reason']}\n\n"
                      "Finding a news angle and drafting. About a minute.", reply_to=msg["message_id"])
        self.make_and_send_draft(self.store.get_note(note_id), chat_id, reply_to=msg["message_id"])

    def handle_callback(self, cq):
        action, draft_id = cq["data"].split(":")
        draft = self.store.get_draft(int(draft_id))
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        if not draft or draft["status"] != "pending":
            return
        chat_id = cq["message"]["chat"]["id"]
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=cq["message"]["message_id"],
           reply_markup={"inline_keyboard": []})
        if action in ("approve", "reject"):
            self.set_draft_status(draft, "approved" if action == "approve" else "rejected", chat_id)
        elif action == "redo":
            self.store.update_draft(draft["id"], "redone")
            note = self.store.get_note(draft["note_id"])
            send(chat_id, f"Redrafting note #{note['id']}...")
            self.make_and_send_draft(note, chat_id, redo_of=draft["text"])

    def claim_review_chat(self, chat_id):
        """The first person to message the bot privately becomes the reviewer."""
        self.store.kv_set("review_chat", chat_id)
        log.info("Review chat set to %s", chat_id)
        send(chat_id, "Connected. Send me a note here (or drop it in the channel) and I'll score it and draft a post.\n\n" + HELP)

    def handle_update(self, upd):
        reviewer = review_chat(self.store)
        if "channel_post" in upd or "message" in upd:
            msg = upd.get("channel_post") or upd.get("message")
            chat_id, private = msg["chat"]["id"], msg["chat"].get("type") == "private"
            if chat_id == NOTES_CHANNEL_ID or chat_id == reviewer:
                self.handle_note(msg)
            elif private and not self.store.kv_get("review_chat") and not os.environ.get("REVIEW_CHAT_ID"):
                self.claim_review_chat(chat_id)
            elif private:
                send(chat_id, "This is a private bot.")
        elif "callback_query" in upd:
            cq = upd["callback_query"]
            if cq.get("message", {}).get("chat", {}).get("id") in (reviewer, NOTES_CHANNEL_ID):
                self.handle_callback(cq)

    def safe_handle(self, upd):
        """handle_update, but tell Meera instead of crashing if something fails."""
        try:
            self.handle_update(upd)
        except Exception as e:
            log.exception("Update %s failed", upd.get("update_id"))
            reason = "Telegram connection dropped" if isinstance(e, requests.RequestException) else str(e)[:200]
            try:
                send(review_chat(self.store), f"Something went wrong ({reason}). Nothing was lost - try again.")
            except Exception:
                pass


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


def due_slot(now):
    """Return a slot key like '2026-09-28 08:00' if a scheduled draft is due now (local runner only)."""
    if now.strftime("%a").lower()[:3] not in DRAFT_DAYS:
        return None
    hh, mm = map(int, DRAFT_TIME.split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < slot or now - slot > dt.timedelta(hours=3):
        return None
    return slot.strftime("%Y-%m-%d %H:%M")
