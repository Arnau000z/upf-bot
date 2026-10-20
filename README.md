# UPF Bot

A Telegram bot where UPF engineering students ask for their timetable in plain words instead of fighting the university portal.

The academic portal is slow and awkward on a phone. What I wanted was to type "what do I have tomorrow?" or "when's my next Algorithms class?" and get an answer. It covers the four engineering degrees at Universitat Pompeu Fabra and was running live for students during the 2025-26 term.

## What it does

- Answers natural-language timetable questions, in a daily or weekly view
- Draws the schedule as a PNG image with Pillow
- Sets up your profile with inline buttons: degree, then year, then group
- Lets an admin re-scrape the portal and refresh the data from a command
- Limits how often each user can hit the LLM, to keep costs down

## The interesting part

Calling an LLM for every message would be slow and wasteful, so most questions never reach it. A rule-based parser handles the common ones locally (around 85% of queries). Only ambiguous or conversational questions go to the LLM, with prompt caching on the big timetable context.

```
scraper.py        scrapes the portal into horarios.json and curriculum.json
query_parser.py   answers common queries locally, no LLM call
bot.py            Telegram handlers, user profiles, LLM fallback
renderer.py       PNG schedule images
```

Per message: load the cached events (1,800+), filter by degree, year and group, keep a 14-day window, then parse locally or ask the LLM.

## Run it

```bash
cp .env.example .env     # ANTHROPIC_API_KEY, TELEGRAM_TOKEN, ADMIN_CHAT_IDS
bash start.sh
tail -f upf-bot.log
```

`whatsapp_webhook.py` is an experimental WhatsApp front end for the same logic; setup notes are in `WHATSAPP.md`.

## Built with

Python, Telegram Bot API, Pillow, an LLM with prompt caching.

## What I'd do next

- Notifications when the schedule changes
- More languages

© 2026 Arnau Mas
