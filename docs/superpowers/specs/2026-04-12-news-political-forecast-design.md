# News & Political Forecast System — Design Spec

## Overview

Extends the existing quantitative investment system with a reactive news and political intelligence layer. Fetches RSS feeds from US and Asia sources every 5 minutes, stores articles in SQLite for 7 days, uses rule-based categorization for fast hotspot detection, and calls the Claude API to synthesize political events into a daily briefing, sector impact forecast, and a `political_risk_score` that feeds into the existing macro regime adjustment. Results are delivered via terminal output and Telegram.

## Goals

1. **Sector/stock impact forecast** — map political and macro news events to affected tickers and ETFs with a directional call
2. **Daily briefing** — structured morning/midday/close summary of political and news events with a directional market call
3. **Political risk signal** — a `political_risk_score` (-1.0 to +1.0) that blends with the existing FRED macro score to adjust equity allocation
4. **Reactive hotspot alerts** — when a volume spike of related articles crosses a threshold, immediately call the LLM and push a Telegram alert

## Architecture

```
RSS feeds (every 5 min via news_poller.py)
  → news_store.py (SQLite .cache/news.db, 7-day retention, dedup by URL hash)
  → political.py (rule-based categorization + severity scoring + hotspot detection)
      ├── severity 3 (hotspot): immediate Claude Haiku call → push Telegram alert
      └── scheduled (8:10 / 12:30 / 17:00 ET): Claude Sonnet call → full briefing
  → forecast.py (Claude API) → { briefing, sector_impacts, political_risk_score }
  → macro.py (political_risk_score blended into macro_risk_adjustment)
  → watchdog.py (prints briefing + alerts in scheduled runs)
  → tg_notifier.py (shared Telegram push helper)
  → tg_bot.py (new /forecast and /hotspots commands)
```

## New Files

| File | Purpose |
|------|---------|
| `news_store.py` | SQLite storage — insert, dedup, retention, query |
| `political.py` | RSS fetching, rule-based event categorization, hotspot detection |
| `forecast.py` | Claude API calls — hotspot analysis + scheduled briefing |
| `news_poller.py` | Background daemon — polls RSS every 5 min, triggers hotspot alerts |
| `tg_notifier.py` | Shared Telegram push helper (used by poller + watchdog) |

## Modified Files

| File | Change |
|------|--------|
| `watchdog.py` | New 3x daily schedule, includes forecast section, political hotspot alerts |
| `macro.py` | `macro_risk_adjustment()` blends FRED + political_risk_score |
| `config.py` | RSS feed list, hotspot thresholds, LLM model config |
| `tg_bot.py` | New `/forecast` and `/hotspots` commands |

## News Sources (RSS)

### US Political & Financial
- Reuters Top News + Reuters Politics
- CNBC Top News + CNBC Politics
- AP Top Stories + AP Business
- Politico (policy/trade/regulation)
- Federal Reserve press releases (federalreserve.gov/feeds)
- MarketWatch

### Asia
- Nikkei Asia (English)
- South China Morning Post (business/markets)
- Singapore Straits Times (business)
- Caixin Global (China economy/markets)
- NHK World (Japan English)

## SQLite Schema (`.cache/news.db`)

```sql
CREATE TABLE articles (
    id          TEXT PRIMARY KEY,   -- SHA256 of URL
    url         TEXT UNIQUE,
    title       TEXT,
    summary     TEXT,
    source      TEXT,               -- "Reuters", "Nikkei Asia", etc.
    region      TEXT,               -- "us" | "asia" | "global"
    fetched_at  DATETIME,
    published_at DATETIME
);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  TEXT REFERENCES articles(id),
    category    TEXT,               -- "tariff"|"fed"|"election"|"geopolitical"|"macro_data"|"earnings"
    keywords    TEXT,               -- JSON array of matched keywords
    severity    INTEGER,            -- 1 (low) / 2 (medium) / 3 (high/hotspot)
    created_at  DATETIME
);

CREATE TABLE llm_analyses (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger               TEXT,     -- "hotspot" | "scheduled"
    category              TEXT,     -- category that triggered (hotspot only)
    input_summary         TEXT,     -- summarized input sent to LLM
    briefing              TEXT,     -- full briefing text
    sector_impacts        TEXT,     -- JSON: {"SPY": "bearish", "XLE": "bullish", ...}
    political_risk_score  REAL,     -- -1.0 to +1.0
    created_at            DATETIME
);
```

**Retention:** Articles older than 7 days deleted on every poll cycle.

## political.py — Event Categorization

### Categories and Sector Mappings

```python
CATEGORIES = {
    "tariff": {
        "keywords": ["tariff", "trade war", "import duty", "sanction", "export ban", "trade deal"],
        "sectors": ["XLY", "XLI", "XLE", "AAPL", "AMZN", "NVDA"],
    },
    "fed": {
        "keywords": ["federal reserve", "fed rate", "fomc", "powell", "interest rate", "quantitative easing", "rate hike", "rate cut"],
        "sectors": ["XLF", "TLT", "IEF", "BIL", "SHY"],
    },
    "election": {
        "keywords": ["election", "congress", "senate", "president", "policy", "regulation", "legislation", "vote"],
        "sectors": ["XLV", "XLE", "XLF", "XLY"],
    },
    "geopolitical": {
        "keywords": ["war", "conflict", "missile", "invasion", "coup", "sanction", "taiwan", "nato", "military", "nuclear"],
        "sectors": ["XLE", "TLT", "BIL", "GLD"],
    },
    "macro_data": {
        "keywords": ["cpi", "inflation", "gdp", "unemployment", "jobs report", "recession", "pce", "retail sales"],
        "sectors": ["SPY", "TLT", "BIL", "XLF"],
    },
    "earnings": {
        "keywords": ["earnings", "revenue", "guidance", "beat", "miss", "outlook", "quarterly results"],
        "sectors": [],  # ticker-specific, resolved at analysis time
    },
}
```

### Severity Rules

| Level | Condition |
|-------|-----------|
| 1 | Single source, no hotspot window breach |
| 2 | 2+ sources on same category OR keywords from 2+ categories in one article |
| 3 | 5+ articles in same category within 30-minute rolling window (hotspot) |

## forecast.py — Claude API Integration

### Hotspot Call (severity 3 trigger)
- **Model:** `claude-haiku-4-5` (fast, cheap)
- **Input:** clustered article titles + summaries from last 30 min window
- **Output:** `{ summary: str (2 sentences), sector_impacts: dict, confidence: "low"|"medium"|"high" }`
- **Rate limit:** max 3 hotspot calls per hour per category (deduped in SQLite)

### Scheduled Briefing Call (8:10 AM / 12:30 PM / 5:00 PM ET)
- **Model:** `claude-sonnet-4-6` (quality)
- **Input:** all events from last 8-hour window, summarized (not raw text) to control tokens
- **Output:** `{ briefing: str, sector_impacts: dict, political_risk_score: float (-1.0 to +1.0) }`
- Pre-market (8:10 AM) input window extended to 14 hours to capture overnight Asia news

### Prompt structure (scheduled)
```
You are analyzing political and macro news for a US equity portfolio.

Recent events (last 8h):
[summarized article titles grouped by category]

Current macro regime: [FRED score + label]
Previous political_risk_score: [last stored value]

Respond in JSON:
{
  "briefing": "2-3 paragraph summary of key developments and market implications",
  "sector_impacts": {"TICKER": "bullish|bearish|neutral", ...},
  "political_risk_score": float between -1.0 (very bearish) and +1.0 (very bullish),
  "key_risks": ["risk 1", "risk 2"]
}
```

## macro.py — Blended Risk Adjustment

```python
def macro_risk_adjustment(target: float) -> float:
    fred_adj = _fred_based_adjustment()           # existing logic, returns 0.4–1.0
    political_score = get_latest_political_score() # from SQLite llm_analyses, default 0.0
    political_adj = 0.7 + 0.3 * political_score   # maps -1→0.4, 0→0.7, +1→1.0
    blended = 0.7 * fred_adj + 0.3 * political_adj
    return target * blended
```

FRED score carries 70% weight, political score 30%.

## news_poller.py — Background Daemon

- Runs continuously as a single process
- Poll loop: fetch all RSS feeds → insert new articles → classify events → check hotspot thresholds → sleep 300s
- On hotspot detected: call `forecast.py` immediately → push Telegram alert via `tg_notifier.py`
- Retention: delete articles older than 7 days on every cycle
- Start: `python3 news_poller.py`
- Optionally wrapped in macOS launchd plist for auto-start on login

## watchdog.py — Updated Schedule

**New cron (weekdays):**
```bash
10 8  * * 1-5  cd /path/to/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
30 12 * * 1-5  cd /path/to/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
0  17 * * 1-5  cd /path/to/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
```

**New sections in watchdog output:**
1. Political hotspot alerts (severity-3 events since last run)
2. Latest briefing (from most recent scheduled LLM analysis in SQLite)
3. Sector impact summary

**New flags:**
```bash
python3 watchdog.py --forecast   # print latest briefing only
python3 forecast.py              # run forecast manually
python3 forecast.py --hotspots   # show recent severity-3 alerts
python3 news_poller.py           # start polling daemon
```

## Telegram Integration

### tg_notifier.py (new shared helper)
- Reads `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` from `.env`
- `send_message(text)` — splits messages >4096 chars, handles rate limiting
- Used by `news_poller.py` (hotspot push) and `watchdog.py` (scheduled push)

### tg_bot.py (new commands)
| Command | Action |
|---------|--------|
| `/forecast` | Latest political briefing + sector impacts |
| `/hotspots` | Recent severity-3 LLM alerts from last 24h |

### Push message formats

**Hotspot alert:**
```
🚨 HOTSPOT: tariff
5 articles in 30min | Confidence: high

[2-sentence LLM summary]

Sectors: XLY ↓  XLI ↓  XLE ↑
```

**Scheduled briefing:**
```
📊 PRE-MARKET BRIEFING — 2026-04-12 08:10 ET

[LLM briefing paragraphs]

Sector Impacts:
  SPY  → neutral
  XLE  → bullish  ↑
  XLY  → bearish  ↓

Political Risk Score: -0.32 (bearish lean)
Key Risks: trade escalation, Fed uncertainty
```

## config.py Additions

```python
# ── News Polling ─────────────────────────────────────────────────
NEWS_POLL_INTERVAL_SECONDS = 300     # 5 minutes
NEWS_RETENTION_DAYS = 7

# ── Hotspot Detection ─────────────────────────────────────────────
HOTSPOT_WINDOW_MINUTES = 30
HOTSPOT_THRESHOLD_COUNT = 5          # articles in window to trigger severity 3
HOTSPOT_MAX_LLM_CALLS_PER_HOUR = 3  # per category

# ── LLM Models ────────────────────────────────────────────────────
LLM_HOTSPOT_MODEL = "claude-haiku-4-5-20251001"
LLM_BRIEFING_MODEL = "claude-sonnet-4-6"
LLM_BRIEFING_SCHEDULE = ["08:10", "12:30", "17:00"]  # ET, weekdays only
```

## Dependencies (additions to requirements.txt)

```
feedparser          # RSS parsing
anthropic           # Claude API
pytz                # timezone handling for ET schedule
```

## Error Handling

- RSS fetch failure: log and skip that source, continue with others
- Claude API failure: log error, use last stored `political_risk_score` (default 0.0 if none)
- SQLite locked: retry with exponential backoff (poller + watchdog may run concurrently)
- Telegram send failure: log, do not crash poller
