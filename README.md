# Skinstinct content bot

MESA · Founder's Office · Cohort C4 · Case 1: *Meera and the Content Backlog*

Meera keeps dropping notes into her Telegram channel. The bot turns the best ones into LinkedIn drafts in her voice and sends them to her in a private Telegram chat for review. **It never publishes to LinkedIn.** Meera approves, edits, and posts the drafts herself. This is the Cut: both no-code consultants offered end-to-end publishing, and she turned it down.

## How it works

| Stage | What happens |
|---|---|
| **Trigger** | Meera sends a note to the bot (private chat or her notes channel). A Mon/Wed/Fri 08:00 IST schedule also drafts from any waiting note |
| **Input** | The note text (or a photo caption) |
| **Context** | `meera_voice_guide.txt` (the Voice Skill, built from her 15 published pieces), her 4 published LinkedIn posts, and one Google News item |
| **Processing** | 1. **Score** (Gemini Flash): 0-10 with a one-line reason. **Below 6:** the bot explains why and stops, with no draft. 2. **Keywords** (Gemini Flash): a short search phrase. 3. **News:** top Google News result (free RSS, no key), giving headline, source, date and link |
| **AI** | **Draft** (Gemini Pro): a 450-600 word post in her voice. The news is used only if it fits naturally. Any number only Meera has becomes a `[MEERA: …]` placeholder |
| **Output** | The draft comes back in the same chat. If it uses the news, it ends with a NEWS SOURCE / FROM / LINK / ⚠ verify block. Meera replies **APPROVE** or **REJECT**, or taps Approve / Redo / Reject |
| **Memory** | Notes, drafts and the Voice Skill are saved in Supabase, with draft status pending → approved / rejected. Rejected notes and drafts are kept, not deleted |

## Files

| File | What it is |
|---|---|
| `core.py` | The pipeline: scoring, Google News, drafting, Telegram replies, APPROVE / REJECT, storage (Supabase or SQLite) |
| `api/webhook.py` | Vercel function. Telegram sends every message here |
| `api/cron.py` | Vercel cron. Drafts from the best waiting note on Mon/Wed/Fri at 08:00 IST |
| `supabase/schema.sql` | Creates the `notes`, `drafts` and `voice_skill` tables (plus two small helper tables) |
| `bot.py` | Local runner and admin commands (`seed-voice`, `set-webhook`, `import`, `draft`) |
| `meera_voice_guide.txt` | The Voice Skill, built from her 4 LinkedIn posts and 11 newsletters. Seeded into `voice_skill` |
| `published_linkedin.txt` | Her 4 published LinkedIn posts, used as style examples |
| `vercel.json` | Function timeout (300 s) and the cron schedule |

## Deploy (Vercel + Supabase)

1. **Supabase:** SQL Editor → New query → paste `supabase/schema.sql` → Run.
2. **Vercel:** Add New → Project → import this repo. Under Environment Variables, add:
   - `TELEGRAM_BOT_TOKEN`
   - `GEMINI_API_KEY`
   - `NOTES_CHANNEL_ID`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `WEBHOOK_SECRET`
   - `CRON_SECRET`

   Then click Deploy.
3. **Seed the Voice Skill and connect Telegram** (from a machine with the same values in `.env`):

```bash
python bot.py seed-voice
```

```bash
python bot.py set-webhook https://YOUR-PROJECT.vercel.app/api/webhook
```

This registers the webhook with Telegram, including the secret token, so only Telegram can call it.

## Run locally instead

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

```bash
./.venv/bin/python bot.py
```

Without `SUPABASE_URL`, it stores everything in a local SQLite file. Local polling only works while no webhook is set; `python bot.py delete-webhook` switches back.

## Using it

- **Send a note:** the bot replies `Score: 9/10 - PASS` (or `NO DRAFT` with the reason). About a minute later, a passing note's draft arrives.
- **APPROVE / REJECT:** updates the latest pending draft. `APPROVE 3` updates draft #3.
- **Buttons:** Approve / Redo (a fresh version) / Reject.
- **Commands:** `/draft 12` drafts note #12, `/queue` shows scored notes waiting for a draft, and `/help` explains how the bot works.

## Backlog (the 60 old notes)

Telegram bots can't read messages sent before they joined a channel, so the old notes have to be imported once:

```bash
./.venv/bin/python bot.py import path/to/result.json
```

This takes a Telegram Desktop export (the channel → ⋮ → Export chat history → JSON). A `notes/` folder with one `.txt` or `.md` file per note also works:

```bash
./.venv/bin/python bot.py import notes/
```

Each note is triaged as it's imported, and a summary of develop / hold / discard counts is printed at the end.

## Guardrails

- **Human in the loop:** nothing is published anywhere. Every draft waits for Meera.
- **No invented facts:** Skinstinct numbers, timelines and product plans come only from her note. Anything missing becomes a `[MEERA: …]` placeholder.
- **Verify the news:** any draft that uses a news item ends with its headline, source, date and link, plus "⚠ Check this before publishing — you are the author of this claim".
- **Voice:** the drafting prompt uses her moves, not her sentences. It is told not to copy lines from her past posts.
- **Secrets:** they live in `.env`, which is git-ignored.

## Limitations

- Voice notes aren't transcribed. Only text messages and photo captions are read.
