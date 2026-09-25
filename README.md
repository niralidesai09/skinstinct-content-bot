# Skinstinct content bot

MESA · Founder's Office · Cohort C4 · Case 1: *Meera and the Content Backlog*

Meera keeps dropping notes into her Telegram channel. The bot turns the best ones into LinkedIn drafts in her voice and sends them to her in a private Telegram chat for review. **It never publishes to LinkedIn.** Meera approves, edits, and posts the drafts herself. This is the Cut: both no-code consultants offered end-to-end publishing, and she turned it down.

## How it works

| Stage | What happens |
|---|---|
| **Trigger** | A new message in the notes channel, the schedule (Mon/Wed/Fri 08:00 IST by default, matching her three-posts-a-week target), or a `/draft` command |
| **Input** | The note text (or a photo caption) |
| **Context** | `meera_voice_guide.txt` (built from her 15 published pieces), her 4 published LinkedIn posts, the note's score and angle, and today's date |
| **Processing** | Notes are saved in SQLite. The scheduler picks the highest-scoring undrafted note (score ≥ `MIN_SCORE`) and never drafts the same note twice |
| **AI** | (1) **Triage** (Gemini Flash): scores each note 1-10 as develop / hold / discard, with a category and a one-line angle. (2) **Draft** (Gemini Pro + Google Search): finds one current news item or data point, then writes a 450-600 word post. Wherever the post needs a number only Meera has, it writes `[MEERA: …]` instead of making one up |
| **Output** | The draft, the news source, and a "check before posting" list arrive in her private chat with the bot, with **Approve / Redo / Skip note** buttons |

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

- **In the channel:** post a note as usual. The bot replies under it with its score, for example `Note #7 - 8/10 - Industry Transparency - worth developing`. Set `TRIAGE_REPLIES=false` to turn these replies off.
- **In the private chat:**
  - `/draft` drafts now from the best waiting note, and `/draft 7` drafts from note #7.
  - `/queue` shows the top waiting notes.
  - `/help` explains how the bot works.
  - **Approve** marks the draft as ready to copy into LinkedIn.
  - **Redo** writes a fresh version with a different opening and news angle.
  - **Skip note** sets the note aside so it won't be picked again.

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
- **Verify the news:** every draft lists its news source, and the "check before posting" list always asks her to verify it.
- **Voice:** the drafting prompt uses her moves, not her sentences. It is told not to copy lines from her past posts.
- **Secrets:** they live in `.env`, which is git-ignored.

## Limitations

- Voice notes aren't transcribed. Only text messages and photo captions are read.
- The schedule only fires while the bot is running.
