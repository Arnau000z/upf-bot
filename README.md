# UPF Bot

Telegram bot that lets students from the 4 Engineering degrees at Universitat Pompeu Fabra query their class schedules in natural language — instead of navigating the university's academic portal.

## Motivation

The UPF academic portal is slow and not mobile-friendly. Students needed a faster way to ask things like *"What do I have tomorrow?"* or *"When is my next Algorithms class?"* — directly from Telegram.

## Features

- Natural language schedule queries powered by **Claude Haiku** (with prompt caching to reduce costs)
- Visual schedule rendering as PNG images (daily and weekly views)
- User profile setup via inline buttons: degree → year → group
- ~85% of queries resolved locally without calling the AI (custom query parser)
- Admin command to re-scrape the university portal and refresh data
- Rate limiting per user to control API costs

## Stack

| Layer | Technology |
|---|---|
| Bot framework | Python + Telegram Bot API (polling) |
| AI | Anthropic Claude Haiku with prompt caching |
| Image rendering | Pillow |
| Data source | UPF academic portal (scraped) |
| Runtime | Linux VPS, nohup background process |

## Architecture

```
scraper.py          ← scrapes UPF portal → horarios.json + curriculum.json
query_parser.py     ← resolves ~85% of queries locally (no AI call)
bot.py              ← Telegram handler, user profiles, Claude fallback
renderer.py         ← generates PNG schedule images with Pillow
```

**Pipeline per message:**
```
horarios.json (1800+ events, cached in RAM)
    ↓ filter by user profile (degree + year + group)
    ↓ filter to 14-day window
    ↓ query_parser → renderer / Claude
```

## Supported degrees

- Enginyeria en Informàtica
- Enginyeria Matemàtica en Ciència de Dades
- Enginyeria en Sistemes Audiovisuals
- Enginyeria de Xarxes de Telecomunicació

## Setup

```bash
cp .env.example .env
# Fill in your credentials in .env
bash start.sh
tail -f upf-bot.log
```

**Required environment variables** (see `.env.example`):
```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_TOKEN=...
ADMIN_CHAT_IDS=your_telegram_id
```

## Status

> **Work in progress.** Currently running live for UPF students in T3 2025-26.
> Planned improvements: multi-language support, notifications for schedule changes, WhatsApp integration.
