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
| **Memory** | Notes and drafts are saved in SQLite with a status (pending → approved / rejected). Rejected notes and drafts are kept, not deleted |

## Files

| File | What it is |
|---|---|
| `bot.py` | The bot: Telegram polling, scheduler, triage, drafting, review buttons, backlog import |
| `meera_voice_guide.txt` | Meera's voice and writing-style guide, built from her 4 LinkedIn posts and 11 newsletters |
| `published_linkedin.txt` | Her 4 published LinkedIn posts, used as style examples |
| `.env.example` | Settings template. Copy it to `.env` and fill it in |
| `requirements.txt` | Python dependencies |

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and make it an **admin** of the notes channel.
2. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`, `NOTES_CHANNEL_ID` and `GEMINI_API_KEY` (get a key from [Google AI Studio](https://aistudio.google.com/apikey)).
3. Install and run:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

```bash
./.venv/bin/python bot.py
```

4. Open the bot in Telegram and tap **Start**. The first person to message it privately becomes the reviewer, and drafts go to that chat from then on.

The bot has to keep running for the schedule to fire, so run it on an always-on machine (a small VPS, or a Railway/Render worker).

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
- The schedule only fires while the bot is running.
