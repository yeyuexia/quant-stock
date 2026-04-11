# Telegram Bot Integration — Design Spec

## Overview

A Telegram bot that serves as a personal interface to the quantitative investment system and a general Claude assistant. Runs locally on macOS as a single Python process.

## Goals

1. **Receive alerts** — Watchdog pushes actionable notifications (stop-loss triggers, macro shifts, big moves) to Telegram on a schedule
2. **Run commands** — Slash commands to invoke stock system functions and get formatted output
3. **Chat with Claude** — Natural language messages routed to Claude Code CLI subprocess for open-ended assistance

## Architecture

Single `tg_bot.py` process using `python-telegram-bot` (v20+, async):

```
Telegram <-> python-telegram-bot <-> Stock system Python functions
                                 <-> claude CLI subprocess (natural language)
```

### Components

- **Command handlers** — Map slash commands to existing stock system functions, capture stdout, return as Telegram messages
- **Chat handler** — Plain text messages sent to `claude --print -p "<message>"` as subprocess with cwd `/Users/zl/works/`, response sent back
- **Scheduler** — Built-in `JobQueue` runs watchdog at 8:30 AM ET on weekdays, sends alerts only when actionable
- **Auth guard** — Only responds to the configured Telegram user ID, silently ignores all others

### Claude Integration

- Invoked via `claude --print -p "<message>"` subprocess
- Working directory: `/Users/zl/works/`
- Timeout: 120 seconds per request
- Claude has access to the full project context in that directory

### Message Handling

- Telegram has a 4096-character message limit
- Long outputs are split into multiple messages
- Stock system output (tables, charts) sent as monospace/code blocks for readability

## Commands

| Command      | Action                                      |
|--------------|---------------------------------------------|
| `/watchdog`  | Run daily watchdog check, return alerts     |
| `/portfolio` | Show current portfolio from portfolio.json  |
| `/run`       | Run full investment system (run.py)         |
| `/screen`    | Run value+quality stock screener            |
| `/macro`     | Run macro regime analysis                   |
| `/sentiment` | Run news & social sentiment scan            |
| `/help`      | List available commands                     |

## Scheduled Alerts

- Watchdog runs automatically at 8:30 AM ET, Monday-Friday
- Only sends messages when there are actionable items:
  - Stop-loss triggers
  - Big overnight/intraday moves (>3%)
  - Macro regime shifts
  - Volume anomalies
- Schedule can be adjusted or disabled

## Auth & Security

- Bot token stored in `.env` (never committed)
- Only the configured `TELEGRAM_USER_ID` can interact
- All other messages silently ignored

## Project Structure

```
/Users/zl/works/tg-bot/
├── tg_bot.py          # Main bot script
├── .env               # TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID
├── requirements.txt   # python-telegram-bot, python-dotenv
└── .gitignore         # Ignore .env
```

- Stock system modules imported by adding `/Users/zl/works/stock` to `sys.path`
- Bot lives in its own subfolder, separate from other projects

## Tech Stack

- **python-telegram-bot** v20+ (async)
- **python-dotenv** for config
- **subprocess** for Claude CLI invocation
- Reuses existing stock system modules: `watchdog.py`, `run.py`, `screener.py`, `macro.py`, `sentiment.py`

## Running

```bash
cd /Users/zl/works/tg-bot
python3 tg_bot.py
```

Runs until Ctrl+C. Can optionally be wrapped in a macOS launchd plist for auto-start.

## Prerequisites

1. Create a Telegram bot via @BotFather, obtain bot token
2. Get your Telegram user ID (e.g. via @userinfobot)
3. Add both to `/Users/zl/works/tg-bot/.env`
4. `pip install -r requirements.txt`
