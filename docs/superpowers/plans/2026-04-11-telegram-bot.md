# Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram bot that provides slash commands for the stock system, natural language chat via Claude Code CLI, and scheduled watchdog alerts.

**Architecture:** Single async Python process using `python-telegram-bot` v20+. Command handlers call stock system functions by importing them and capturing stdout. Natural language messages are forwarded to `claude --print` subprocess. A `JobQueue` scheduler runs the watchdog at 8:30 AM ET on weekdays and pushes alerts.

**Tech Stack:** python-telegram-bot v20+, python-dotenv, asyncio subprocess for Claude CLI

---

### Task 1: Project scaffold

**Files:**
- Create: `/Users/zl/works/tg-bot/requirements.txt`
- Create: `/Users/zl/works/tg-bot/.gitignore`
- Create: `/Users/zl/works/tg-bot/.env.example`

- [ ] **Step 1: Create the project directory**

```bash
mkdir -p /Users/zl/works/tg-bot
```

- [ ] **Step 2: Create requirements.txt**

Create `/Users/zl/works/tg-bot/requirements.txt`:

```
python-telegram-bot[job-queue]>=20.0
python-dotenv>=1.0.0
```

The `[job-queue]` extra installs APScheduler, needed for the scheduled watchdog alerts.

- [ ] **Step 3: Create .gitignore**

Create `/Users/zl/works/tg-bot/.gitignore`:

```
.env
__pycache__/
*.pyc
```

- [ ] **Step 4: Create .env.example**

Create `/Users/zl/works/tg-bot/.env.example`:

```
TELEGRAM_BOT_TOKEN=your-bot-token-from-botfather
TELEGRAM_USER_ID=your-numeric-telegram-user-id
```

- [ ] **Step 5: Install dependencies**

```bash
cd /Users/zl/works/tg-bot && pip install -r requirements.txt
```

- [ ] **Step 6: Initialize git repo and commit**

```bash
cd /Users/zl/works/tg-bot
git init
git add requirements.txt .gitignore .env.example
git commit -m "chore: scaffold tg-bot project"
```

---

### Task 2: Auth guard and bot skeleton

**Files:**
- Create: `/Users/zl/works/tg-bot/tg_bot.py`

- [ ] **Step 1: Create the bot skeleton with auth guard**

Create `/Users/zl/works/tg-bot/tg_bot.py`:

```python
#!/usr/bin/env python3
"""
Telegram bot for the quantitative investment system.
Provides slash commands, scheduled alerts, and Claude chat.

Usage:
  1. Copy .env.example to .env and fill in your tokens
  2. python3 tg_bot.py
"""
import os
import sys
import io
import asyncio
import logging
from functools import wraps
from contextlib import redirect_stdout

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_USER_ID = int(os.environ["TELEGRAM_USER_ID"])
STOCK_DIR = os.path.join(os.path.dirname(__file__), "..", "stock")
WORK_DIR = os.path.join(os.path.dirname(__file__), "..")

# Add stock dir to path so we can import its modules
sys.path.insert(0, os.path.abspath(STOCK_DIR))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def auth(func):
    """Decorator: only allow the configured user."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_USER_ID:
            return  # silently ignore
        return await func(update, context)
    return wrapper


async def send_long_message(update: Update, text: str):
    """Send a message, splitting into chunks if it exceeds Telegram's 4096 char limit."""
    max_len = 4000  # leave some margin
    if len(text) <= max_len:
        await update.message.reply_text(text)
        return
    # Split on newlines to avoid breaking mid-line
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > max_len:
            await update.message.reply_text(chunk)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk)


def capture_stdout(func, *args, **kwargs):
    """Call a function and capture its print output as a string."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = func(*args, **kwargs)
    return buf.getvalue(), result


# ── Placeholder for command handlers (Task 3) ──
# ── Placeholder for chat handler (Task 4) ──
# ── Placeholder for scheduler (Task 5) ──


@auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Stock Bot Commands:\n"
        "/portfolio - Current portfolio status\n"
        "/watchdog - Run daily watchdog check\n"
        "/run - Run full investment system\n"
        "/screen - Value+quality stock screener\n"
        "/macro - Macro regime analysis\n"
        "/sentiment - News & social sentiment\n"
        "/help - Show this message\n"
        "\nOr just send any message to chat with Claude."
    )
    await update.message.reply_text(text)


@auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Stock assistant ready. Send /help to see commands, or just chat."
    )


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Command handlers will be added in Task 3
    # Chat handler will be added in Task 4
    # Scheduler will be set up in Task 5

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test**

Create a `.env` with a test token (or real token if available) and run:

```bash
cd /Users/zl/works/tg-bot && python3 -c "import tg_bot; print('import ok')"
```

Expected: `import ok` (verifies syntax and imports work)

- [ ] **Step 3: Commit**

```bash
cd /Users/zl/works/tg-bot
git add tg_bot.py
git commit -m "feat: add bot skeleton with auth guard and help command"
```

---

### Task 3: Stock system command handlers

**Files:**
- Modify: `/Users/zl/works/tg-bot/tg_bot.py`

These handlers import functions from the stock system, run them in a thread (to avoid blocking the async event loop), capture their stdout, and send the output back.

- [ ] **Step 1: Add the command handlers**

Add the following after the `capture_stdout` function and before `cmd_help`:

```python
def run_in_thread(func, *args, **kwargs):
    """Run a blocking stock function in a thread and capture stdout."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(capture_stdout, func, *args, **kwargs)
        return future.result(timeout=120)


@auth
async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Checking portfolio...")
    try:
        from watchdog import load_portfolio, check_portfolio_status, header
        portfolio = load_portfolio()
        if not portfolio["positions"]:
            await update.message.reply_text("No portfolio found.")
            return

        rows, total_value, total_pnl, total_pnl_pct, cash = check_portfolio_status(portfolio)
        lines = []
        for r in rows:
            icon = "+" if r["pnl"] >= 0 else "-"
            lines.append(
                f"{icon} {r['ticker']:6s} {r['shares']:3d}x ${r['current']:>8.2f} = ${r['value']:>9.2f} "
                f"P&L: ${r['pnl']:>+8.2f} ({r['pnl_pct']:>+.1f}%)"
            )
        lines.append(f"\nCash:      ${cash:>10,.2f}")
        lines.append(f"Portfolio: ${total_value:>10,.2f}")
        icon = "+" if total_pnl >= 0 else "-"
        lines.append(f"Total P&L: {icon} ${abs(total_pnl):>10,.2f} ({total_pnl_pct:>+.1f}%)")
        await send_long_message(update, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@auth
async def cmd_watchdog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Running watchdog...")
    try:
        from watchdog import run_watchdog
        loop = asyncio.get_event_loop()
        output, _ = await loop.run_in_executor(None, capture_stdout, run_watchdog, False)
        await send_long_message(update, output or "Watchdog completed (no output).")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@auth
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Running full investment system... (this takes a minute)")
    try:
        # Run as subprocess since run.py imports many modules and prints a lot
        proc = await asyncio.create_subprocess_exec(
            sys.executable, os.path.join(STOCK_DIR, "run.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=STOCK_DIR,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode()
        if proc.returncode != 0:
            output += f"\n\nSTDERR:\n{stderr.decode()}"
        await send_long_message(update, output or "Run completed (no output).")
    except asyncio.TimeoutError:
        await update.message.reply_text("Timed out after 5 minutes.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@auth
async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Running stock screener...")
    try:
        from screener import screen_stocks
        loop = asyncio.get_event_loop()
        output, df = await loop.run_in_executor(None, capture_stdout, screen_stocks)
        # Build a concise table from the returned DataFrame
        if df is not None and not df.empty:
            lines = ["Top Screened Stocks:\n"]
            for _, row in df.head(10).iterrows():
                lines.append(
                    f"#{row['rank']:2d} {row['ticker']:6s} "
                    f"${row['price']:>8.2f}  "
                    f"P/E:{row['pe'] or 0:>5.1f}  "
                    f"Score:{row['composite']:.3f}"
                )
            await send_long_message(update, "\n".join(lines))
        else:
            await update.message.reply_text("No screening results.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@auth
async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Running macro analysis...")
    try:
        from macro import macro_regime_score, macro_risk_adjustment
        loop = asyncio.get_event_loop()

        def _run_macro():
            result = macro_regime_score()
            adj = macro_risk_adjustment(1.0)
            return result, adj

        output, (result, adj) = await loop.run_in_executor(None, capture_stdout, _run_macro)

        score = result["score"]
        regime = result["regime"]
        lines = [
            f"Macro Regime: {regime.upper()}",
            f"Score: {score:+.3f}",
            f"Risk Adjustment: {adj*100:.0f}%\n",
        ]
        for name, ind in result["indicators"].items():
            lines.append(f"  {name:18s} {ind['signal']:+.1f}  {ind['label']}")
        await send_long_message(update, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@auth
async def cmd_sentiment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching sentiment...")
    try:
        from sentiment import get_market_hotspots
        loop = asyncio.get_event_loop()

        def _run():
            return get_market_hotspots()

        _, hotspots = await loop.run_in_executor(None, capture_stdout, _run)

        mood = hotspots["market_mood"]
        label = hotspots["mood_label"]
        lines = [
            f"Market Mood: {label} ({mood:+.2f})",
            f"Sources: {hotspots['news_count']} news, {hotspots['reddit_count']} Reddit\n",
        ]

        # Portfolio alerts
        alerts = hotspots.get("portfolio_alerts", [])
        if alerts:
            lines.append(f"Portfolio Alerts ({len(alerts)}):")
            for a in alerts[:8]:
                icon = "+" if a["sentiment"] == "bullish" else "-" if a["sentiment"] == "bearish" else "~"
                lines.append(f"  {icon} [{a['ticker']}] {a['headline'][:60]}")
            lines.append("")

        # Ticker buzz
        buzz = hotspots.get("ticker_buzz")
        if buzz is not None and not buzz.empty:
            lines.append("Top Buzz:")
            for _, row in buzz.head(8).iterrows():
                lines.append(f"  {row['ticker']:6s} {row['mentions']}x  sent:{row['avg_sentiment']:+.2f}")

        await send_long_message(update, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
```

- [ ] **Step 2: Register the command handlers in main()**

Replace the comment `# Command handlers will be added in Task 3` with:

```python
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("watchdog", cmd_watchdog))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("macro", cmd_macro))
    app.add_handler(CommandHandler("sentiment", cmd_sentiment))
```

- [ ] **Step 3: Verify imports**

```bash
cd /Users/zl/works/tg-bot && python3 -c "import tg_bot; print('import ok')"
```

Expected: `import ok`

- [ ] **Step 4: Commit**

```bash
cd /Users/zl/works/tg-bot
git add tg_bot.py
git commit -m "feat: add stock system command handlers"
```

---

### Task 4: Claude chat handler

**Files:**
- Modify: `/Users/zl/works/tg-bot/tg_bot.py`

- [ ] **Step 1: Add the chat handler**

Add the following after the command handlers and before `cmd_help`:

```python
@auth
async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward natural language messages to Claude Code CLI."""
    user_msg = update.message.text
    if not user_msg:
        return

    await update.message.reply_text("Thinking...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--print", "-p", user_msg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.abspath(WORK_DIR),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        response = stdout.decode().strip()
        if not response:
            response = f"(no output)\nstderr: {stderr.decode().strip()}"
        await send_long_message(update, response)
    except asyncio.TimeoutError:
        await update.message.reply_text("Claude timed out after 2 minutes.")
    except FileNotFoundError:
        await update.message.reply_text(
            "Claude CLI not found. Make sure 'claude' is in PATH."
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
```

- [ ] **Step 2: Register the chat handler in main()**

Replace the comment `# Chat handler will be added in Task 4` with:

```python
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
```

- [ ] **Step 3: Commit**

```bash
cd /Users/zl/works/tg-bot
git add tg_bot.py
git commit -m "feat: add Claude chat handler via CLI subprocess"
```

---

### Task 5: Scheduled watchdog alerts

**Files:**
- Modify: `/Users/zl/works/tg-bot/tg_bot.py`

- [ ] **Step 1: Add the scheduled alert function**

Add the following after the chat handler:

```python
async def scheduled_watchdog(context: ContextTypes.DEFAULT_TYPE):
    """Run watchdog and send alerts to the user. Called by JobQueue on schedule."""
    try:
        from watchdog import (
            load_portfolio, check_price_moves, check_volume,
            check_macro_shift, check_news, check_rebalance,
            check_portfolio_status,
        )

        portfolio = load_portfolio()
        if not portfolio["positions"]:
            return  # no portfolio, nothing to alert on

        all_alerts = []
        all_alerts.extend(check_price_moves(portfolio))
        all_alerts.extend(check_volume(portfolio))

        macro_alerts, _ = check_macro_shift()
        all_alerts.extend(macro_alerts)
        all_alerts.extend(check_news(portfolio))
        all_alerts.extend(check_rebalance(portfolio))

        if not all_alerts:
            return  # no alerts, stay silent

        # Build summary
        _, total_value, total_pnl, total_pnl_pct, cash = check_portfolio_status(portfolio)

        lines = [
            "Daily Watchdog Alert\n",
            f"Portfolio: ${total_value:>,.2f} ({total_pnl_pct:>+.1f}%)\n",
        ]

        critical = [a for a in all_alerts if "CRITICAL" in a[0]]
        warnings = [a for a in all_alerts if "WARNING" in a[0]]
        infos = [a for a in all_alerts if "INFO" in a[0]]

        for a in critical + warnings + infos:
            lines.append(f"{a[0]} [{a[1]}] {a[2]}")

        if critical:
            lines.append(f"\n{len(critical)} CRITICAL alert(s) - ACTION REQUIRED!")

        await context.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text="\n".join(lines),
        )
    except Exception as e:
        logger.error(f"Scheduled watchdog error: {e}")
        await context.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=f"Watchdog error: {e}",
        )
```

- [ ] **Step 2: Set up the scheduler in main()**

Add this import at the top of the file with the other imports:

```python
from datetime import time as dt_time
import pytz
```

Replace the comment `# Scheduler will be set up in Task 5` with:

```python
    # Schedule watchdog at 8:30 AM ET, Monday-Friday
    et = pytz.timezone("US/Eastern")
    app.job_queue.run_daily(
        scheduled_watchdog,
        time=dt_time(hour=8, minute=30, tzinfo=et),
        days=(0, 1, 2, 3, 4),  # Monday=0 through Friday=4
        name="daily_watchdog",
    )
    logger.info("Scheduled daily watchdog at 8:30 AM ET, Mon-Fri")
```

- [ ] **Step 3: Add pytz to requirements.txt**

Update `/Users/zl/works/tg-bot/requirements.txt`:

```
python-telegram-bot[job-queue]>=20.0
python-dotenv>=1.0.0
pytz>=2023.3
```

- [ ] **Step 4: Install the new dependency**

```bash
cd /Users/zl/works/tg-bot && pip install -r requirements.txt
```

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/tg-bot
git add tg_bot.py requirements.txt
git commit -m "feat: add scheduled daily watchdog alerts at 8:30 AM ET"
```

---

### Task 6: End-to-end test with real bot

**Files:** None (manual testing)

- [ ] **Step 1: Set up .env**

Copy `.env.example` to `.env` and fill in:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_USER_ID` — from @userinfobot (send it any message, it replies with your ID)

```bash
cp /Users/zl/works/tg-bot/.env.example /Users/zl/works/tg-bot/.env
# Edit .env with real values
```

- [ ] **Step 2: Start the bot**

```bash
cd /Users/zl/works/tg-bot && python3 tg_bot.py
```

Expected: `Bot starting...` log message, process stays running.

- [ ] **Step 3: Test in Telegram**

Open Telegram and message your bot. Test each command:

1. `/start` — should reply "Stock assistant ready..."
2. `/help` — should list all commands
3. `/portfolio` — should show current holdings and P&L
4. `/macro` — should show macro regime analysis
5. `/screen` — should show top screened stocks
6. `/sentiment` — should show market mood and alerts
7. `/watchdog` — should run full watchdog check
8. `/run` — should run the full investment system (takes ~1 min)
9. Send a plain text message like "what's the weather?" — should get a Claude response
10. Send "show me the current portfolio allocation" — Claude should answer using project context

- [ ] **Step 4: Verify auth guard**

Have someone else (or a different Telegram account) message the bot. It should silently ignore them.

- [ ] **Step 5: Final commit**

```bash
cd /Users/zl/works/tg-bot
git add -A
git commit -m "chore: finalize telegram bot for end-to-end use"
```
