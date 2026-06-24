# SEPA Take-Profit (Phase 2) — Design

**Date:** 2026-05-18
**Status:** Spec, awaiting implementation plan
**Scope:** Add two Minervini SEPA exit rules on top of Phase 1: **failed-breakout** (3-day rule: any close below the entry pivot within 3 trading days triggers a full exit) and **climax** (return + range + volume triple condition: sell 50% of the remaining position into strength and tighten the trailing stop). Phase 1 (R-multiple scale-out + EMA trail) is untouched architecturally — Phase 2 only adds new checks ahead of Phase 1's existing rules.

**Prerequisite:** Phase 1 already merged on `main` (see `docs/superpowers/specs/2026-05-17-sepa-take-profit-phase1-design.md`).

---

## 1. Motivation

Phase 1 protects winners but does nothing about the two most common ways a SEPA setup goes wrong:

1. **Failed breakout** — a stock breaks out of a base, you buy at the pivot, and within 1–3 days price falls back below the pivot. Minervini treats this as a hard signal: the setup is invalid; cut immediately, don't wait for the stop-loss. Without this rule, a failed breakout currently drifts down to the −8% stop-loss (or worse, the ATR-tightened equivalent), giving back ~6–8% of capital on a setup the market told you was broken on day 1.
2. **Climax / blow-off** — a position runs 25%+ in 8 days with range expansion and a volume signature. Minervini's "sell into strength" rule: take half off before the inevitable reversion. Phase 1's R-multiple scale-out captures part of this, but it triggers only at the specific 2R and 3R thresholds. A climax that erupts past 5R in two days outpaces the scale-out.

Both rules apply only to **core-tranche stock-sleeve** entries (screener-driven). ETF entries from the momentum module have no pivot concept; aggressive-tranche leveraged ETFs have their own decay-driven rules already.

---

## 2. Goals & non-goals

### Goals
- Per-position **failed-breakout** check during the 3 trading days after entry: any daily close below the entry pivot → cancel any pending Phase 1 partial sells AND submit a full exit.
- Per-position **climax** check: when all three conditions (cumulative return, average daily range, recent volume spike) are met simultaneously, sell 50% of current remaining position, replace any existing trailing-stop with a tighter one (default 6%), and mark `climax_fired=True` to disable subsequent Phase 1 R-multiple scale-out on this position.
- Persist the entry pivot at rebalance time so the watchdog has a reference point.
- Garbage-collect the pivot sidecar when positions exit.
- All parameters exposed in `config.py` for tuning.

### Non-goals
- Stage-3 transition rule (Phase 3 or later).
- Aggressive-tranche SEPA application.
- Quant-review subagent overrides for Phase 2 parameters (kept static in v1).
- Automatic pivot detection for ETF entries (out of scope — ETF entries are not eligible for failed-breakout).
- Re-enabling R-multiple after climax fires (once climax fires, the remaining position is governed by tighter trailing + MA-trail only).
- Persisting climax detail (`climax_fired_at`, climax-condition values) beyond the boolean flag — known limitation, can be added in a follow-up if needed for audit.

---

## 3. Architecture

```
sepa_exits.py  (Phase 1 + NEW)
  pure compute:
    initial_r, r_multiple, next_r_tier_action,           ← Phase 1
    ma_break, ma_trail_should_exit,                       ← Phase 1
    failed_breakout, climax_check                          ← Phase 2 NEW

rebalancer._build_core_targets / _write_pending_plan
  for each screener-driven entry:
    write .cache/entry_pivots.json
      { "AAPL": { "pivot": <base_hi>, "entry_date": "<UTC>" } }

orders.sync_state
  initial_*  snapshot                                     ← Phase 1
  r_tier_filled  append-on-qty-drop                        ← Phase 1
  climax_fired  initialize False, gate r_tier appends     ← Phase 2 NEW

watchdog.check_sepa_exits  (priority order)
  per core position:
    1. failed_breakout?    yes → cancel pending + submit_exit
    2. climax?              yes → cancel pending + sell 50% + tighter trail + climax_fired=True
    3. R-multiple?          (gated by !climax_fired)        ← Phase 1
    4. MA-trail?            (gated by 3R-filled OR climax_fired) ← Phase 1 + extended
  end of pass:
    GC entry_pivots.json — remove keys for symbols no longer held
```

The architectural pattern is identical to Phase 1: pure-compute helpers in `sepa_exits.py`, side-effecting helpers in `orders.py`, orchestration in `watchdog.py`. The only new persistence is the `.cache/entry_pivots.json` sidecar and the `climax_fired` field on each position.

---

## 4. Components

### 4.1 `.cache/entry_pivots.json` (new sidecar)

```jsonc
{
  "AAPL": {
    "pivot": 200.5,
    "entry_date": "2026-05-18"        // UTC date
  },
  "NVDA": {
    "pivot": 150.0,
    "entry_date": "2026-05-18"
  }
}
```

- **Written by**: `rebalancer._build_core_targets` after calling `screen_stocks()` — for each of the top 3 picks that become a new buy intent.
- **Read by**: `watchdog.check_sepa_exits` (failed-breakout check).
- **GC**: end of each `watchdog.check_sepa_exits` pass, remove keys for any symbol not present in `snap.by_tranche("core")`.
- **Overwrite semantics**: if rebalancer re-enters a previously exited position, the new pivot overwrites the old; the failed-breakout window restarts from the new `entry_date`.
- **ETF entries skipped**: the rebalancer's momentum path does not write to this sidecar (no pivot concept).

`base_hi` is sourced from `screener.screen_stocks()`. The screener's `_detect_base` already computes `hi` internally (`screener.py:_detect_base` returns `base_weeks`, `depth`, `tightness`); `screen_stocks` will expose `base_hi` as a new column on the returned DataFrame (small addition).

### 4.2 `portfolio.json` schema extension (one new field)

```jsonc
{
  ...(existing fields + Phase 1 initial_* + r_tier_filled)...,
  "climax_fired": false        // NEW: True after climax check sells half
}
```

**`sync_state` rules**:
- New core position: `climax_fired = False`.
- Subsequent runs: preserve whatever was last persisted. Never reset to False unless the position is dropped (exits Alpaca) and later re-entered.
- **Gate on `r_tier_filled` append logic**: when `climax_fired == True`, skip the qty-drop → tier-append heuristic entirely. Climax's own 50% partial sell would otherwise incorrectly trigger "2R" (and possibly "3R") appends because qty drops past those thresholds.

### 4.3 `sepa_exits.py` new functions (pure compute)

```python
def failed_breakout(position: dict, pivots: dict, closes: pd.Series,
                    *, today: dt.date,
                    window_days: int = 3) -> bool:
    """True if any close on or after entry_date and on or before today
    is below the entry pivot, and the (today − entry_date) trading-day
    count is ≤ window_days.

    `closes` is a daily-indexed Series with a DatetimeIndex covering the
    period from at least `entry_date` through `today`. The function:
      1. Looks up `pivots[position['symbol']]` → if absent, returns False.
      2. Reads `entry_date` from the pivot record.
      3. Counts the number of close bars between entry_date (exclusive) and
         today (inclusive). If that count exceeds window_days, returns False.
      4. For each in-window close (entry_date < bar_date ≤ today), if any
         close < pivot price, returns True.
      5. Otherwise returns False.

    The window-day count uses observed bars in `closes`, not calendar days,
    so weekends and exchange holidays are naturally handled.
    """


def climax_check(ohlcv: pd.DataFrame, *,
                 return_lookback: int = 8,
                 return_threshold: float = 0.25,
                 range_lookback: int = 20,
                 range_multiplier: float = 2.0,
                 volume_lookback: int = 20,
                 volume_multiplier: float = 2.0,
                 volume_recent_days: int = 3) -> bool:
    """All three conditions ANDed:
      1. Return: (last_close / closes[-return_lookback-1]) − 1 ≥ return_threshold
      2. Range expansion:
         mean(daily_range over last range_lookback days) ≥
         range_multiplier × mean(daily_range over prior range_lookback days)
      3. Volume spike:
         max(volume over last volume_recent_days) ≥
         volume_multiplier × mean(volume over prior volume_lookback days,
                                  excluding the volume_recent_days)

    Returns False on insufficient data (fewer than
    max(return_lookback+1, 2*range_lookback, volume_lookback + volume_recent_days)
    aligned bars).
    """
```

Both functions are pure: no I/O, no broker access. Failed-breakout takes the pivot dict already loaded by the caller (decouples test setup from filesystem). Climax takes the OHLCV DataFrame already fetched by the caller.

### 4.4 `watchdog.check_sepa_exits` extension

The Phase 1 implementation loops over `snap.by_tranche("core")` and runs R-multiple + MA-trail per position. Phase 2 prepends two checks at the **start** of the per-position body, in priority order, and adds a GC step at the end:

```python
def check_sepa_exits(snap, broker):
    notifications: list = []
    if not getattr(config, "SEPA_ENABLED", False):
        return notifications

    import sepa_exits
    import data

    pivots = _load_entry_pivots()
    today = dt.datetime.now(dt.timezone.utc).date()

    for pos in snap.by_tranche("core"):
        symbol = pos["symbol"]
        if pos.get("initial_stop_price") is None:
            continue
        try:
            current_price = float(broker._latest_price(symbol))
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: no latest price ({e})")
            continue

        # 1. Failed-breakout (highest priority)
        try:
            ohlcv = data.fetch_ohlcv([symbol], period=config.SEPA_MA_HISTORY)
            closes = ohlcv["Close"][symbol].dropna()
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: closes fetch failed: {e}")
            continue

        if sepa_exits.failed_breakout(pos, pivots, closes, today=today,
                                      window_days=config.SEPA_FAILED_BREAKOUT_WINDOW_DAYS):
            _cancel_pending_partials(symbol)
            orders.cancel_position_trailing(symbol, broker=broker)
            orders.submit_exit(symbol, reason="sepa-failed-breakout", broker=broker)
            _sepa_notify(
                f"⚠ SEPA failed-breakout — {symbol}\n"
                f"Close ${float(closes.iloc[-1]):.2f} < entry pivot "
                f"${pivots[symbol]['pivot']:.2f}; full exit.",
                notifications,
            )
            continue

        # 2. Climax (only if not already fired)
        if not pos.get("climax_fired"):
            if sepa_exits.climax_check(
                ohlcv,
                return_lookback=config.SEPA_CLIMAX_RETURN_LOOKBACK,
                return_threshold=config.SEPA_CLIMAX_RETURN_THRESHOLD,
                range_lookback=config.SEPA_CLIMAX_RANGE_LOOKBACK,
                range_multiplier=config.SEPA_CLIMAX_RANGE_MULTIPLIER,
                volume_lookback=config.SEPA_CLIMAX_VOLUME_LOOKBACK,
                volume_multiplier=config.SEPA_CLIMAX_VOLUME_MULTIPLIER,
                volume_recent_days=config.SEPA_CLIMAX_VOLUME_RECENT_DAYS,
            ):
                _cancel_pending_partials(symbol)
                orders.cancel_position_trailing(symbol, broker=broker)

                # Sell 50% of CURRENT remaining (not initial)
                half_mv = float(pos["market_value"]) * 0.5
                cid = orders._make_cid("core", "sepa-climax", symbol, today)
                sell_intent = orders.OrderIntent(
                    symbol=symbol, notional=round(half_mv, 2), side="sell",
                    reason="sepa-climax", tranche="core", client_order_id=cid,
                )
                orders.execute_plan(
                    orders.OrderPlan(buys=[], sells=[sell_intent], holds=[]),
                    broker=broker, reason="sepa-climax",
                )

                # Tighter trailing on remaining (estimated) qty
                remaining_qty = float(pos["shares"]) * 0.5
                trail_cid = orders._make_cid("core", "climax-trail", symbol, today)
                try:
                    broker.submit_trailing_stop(
                        symbol, qty=remaining_qty,
                        trail_percent=config.SEPA_CLIMAX_TRAIL_PCT,
                        client_order_id=trail_cid,
                    )
                except Exception as e:
                    notifications.append(f"⚠ SEPA {symbol}: climax re-trail failed: {e}")

                _set_climax_fired(symbol)
                _sepa_notify(
                    f"🔥 SEPA climax — {symbol}\n"
                    f"Triple condition met; sold ~50% (${half_mv:,.0f}) at "
                    f"${current_price:.2f}; tightened trail to "
                    f"{config.SEPA_CLIMAX_TRAIL_PCT*100:.0f}%; "
                    f"R-multiple scale-out disabled.",
                    notifications,
                )
                continue

        # 3. R-multiple (Phase 1) — gated by !climax_fired
        if not pos.get("climax_fired"):
            action = sepa_exits.next_r_tier_action(pos, current_price)
            if action is not None:
                # ... existing Phase 1 R-multiple body ...
                continue

        # 4. MA-trail — original gating (3R filled) OR climax_fired
        final_label = f"{int(config.SEPA_R_TIERS[-1][0])}R"
        if final_label in (pos.get("r_tier_filled") or []) or pos.get("climax_fired"):
            if sepa_exits.ma_trail_should_exit(pos, closes):
                orders.submit_exit(symbol, reason="sepa-21EMA-break", broker=broker)
                _sepa_notify(
                    f"📉 SEPA 21EMA break — {symbol}\n"
                    f"Last close ${float(closes.iloc[-1]):.2f} below 21EMA; "
                    f"exiting remaining shares.",
                    notifications,
                )

    # GC: remove pivot records for symbols no longer held.
    held_symbols = {p["symbol"] for p in snap.by_tranche("core")}
    pruned = {k: v for k, v in pivots.items() if k in held_symbols}
    if len(pruned) != len(pivots):
        _save_entry_pivots(pruned)

    return notifications
```

New helpers in `watchdog.py`:
- `_load_entry_pivots()` / `_save_entry_pivots(d)` — JSON read/write to `config.ENTRY_PIVOTS_PATH`. Defensive: missing file → empty dict; malformed → empty dict + alert.
- `_cancel_pending_partials(symbol)` — read `.cache/pending_plan.json`; if any intent on this symbol is unfilled, drop it from the plan and re-persist. Idempotent.
- `_set_climax_fired(symbol)` — load `portfolio.json`, find the symbol's entry, set `climax_fired=True`, re-save. Standalone (not part of `sync_state`).

### 4.5 `rebalancer.py` extension

In `_build_core_targets` (or just before `reconcile_to_targets` returns), after calling `screen_stocks()`:

```python
df = screen_stocks()
if df is not None and not df.empty:
    top = df.head(3)
    per = stock_pct / max(1, len(top))
    pivots = _load_entry_pivots()
    today_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for _, row in top.iterrows():
        targets[row["ticker"]] = targets.get(row["ticker"], 0.0) + per
        # Phase 2: persist the entry pivot.
        if "base_hi" in row and pd.notna(row["base_hi"]):
            pivots[row["ticker"]] = {
                "pivot": float(row["base_hi"]),
                "entry_date": today_str,
            }
    _save_entry_pivots(pivots)
```

The momentum/ETF path is unchanged — pivots are written only for screener picks.

**`screener.screen_stocks()` change**: add `base_hi` to the columns of the returned DataFrame. `_detect_base` already computes `hi` internally — promote it from the local variable to the returned dict. Trivial change.

### 4.6 `config.py` additions

```python
# SEPA Phase 2 — failed-breakout
SEPA_FAILED_BREAKOUT_WINDOW_DAYS = 3
ENTRY_PIVOTS_PATH = os.path.join(os.path.dirname(__file__),
                                  ".cache", "entry_pivots.json")

# SEPA Phase 2 — climax
SEPA_CLIMAX_RETURN_LOOKBACK = 8
SEPA_CLIMAX_RETURN_THRESHOLD = 0.25
SEPA_CLIMAX_RANGE_LOOKBACK = 20
SEPA_CLIMAX_RANGE_MULTIPLIER = 2.0
SEPA_CLIMAX_VOLUME_LOOKBACK = 20
SEPA_CLIMAX_VOLUME_MULTIPLIER = 2.0
SEPA_CLIMAX_VOLUME_RECENT_DAYS = 3
SEPA_CLIMAX_TRAIL_PCT = 0.06       # 6% — half of default core trail
```

No entries in `_OVERRIDE_SCHEMA` for v1 — let the parameters stabilize before exposing to the quant subagent.

---

## 5. Priority and state interaction

When multiple SEPA rules apply to the same position on the same watchdog run, priority order (top-down; first match wins, `continue` to next position):

1. **Failed-breakout** — strongest signal. Position is invalid; exit immediately, override anything else queued.
2. **Climax** — sell-into-strength opportunity. Half off, tighten the rest, gate off R-multiple.
3. **R-multiple scale-out** — Phase 1 logic, gated by `!climax_fired`.
4. **MA-trail** — Phase 1 logic, extended trigger: fires when **either** the final R-tier label is in `r_tier_filled` **or** `climax_fired == True`. Climax's tightened trailing-stop is the primary protection; MA-trail is the backstop.

State machine implications:

```
Entry → climax_fired=False, r_tier_filled=[]
  │
  ├── [Day 1-3] close < pivot → FAILED-BREAKOUT
  │    └── cancel pending, full exit, sync_state drops position
  │
  ├── [any day] climax triple → CLIMAX
  │    └── sell 50%, tighter trail, climax_fired=True
  │         │
  │         ├── R-multiple permanently disabled
  │         └── MA-trail active: close < 21EMA → full exit
  │
  └── [any day] price ≥ 2R → R-MULTIPLE (Phase 1 unchanged)
       └── ... (Phase 1 path)
```

A position can transition Entry → Failed-breakout, OR Entry → Climax → MA-trail/stop, OR Entry → R-multiple → MA-trail/stop. It cannot transition through both Climax and full R-multiple (climax gates R-multiple off).

---

## 6. Edge cases

| Condition | Behavior |
|---|---|
| Failed-breakout window (Day 4+) | `failed_breakout` returns False; R-multiple/climax/MA-trail proceed normally |
| Symbol not in entry_pivots.json (ETF or external position) | `failed_breakout` returns False; other rules unaffected |
| Climax fires same day as R-multiple 2R threshold reached | Priority sends control to climax branch; R-multiple body is skipped (continue) |
| Climax fires same day as failed-breakout | Failed-breakout body runs first; position is fully exited; climax body unreachable that turn |
| Climax fires AND sync_state observes qty drop to 1/2 before next watchdog | `climax_fired==True` gates the r_tier_filled append heuristic — no spurious "2R" append from the climax partial sell |
| GC removes pivot for a position that re-enters next day | Rebalancer overwrites with fresh `pivot` + `entry_date`; window restarts |
| `base_hi` missing from screener output (legacy / errored) | rebalancer logs a warning, omits the pivot entry for that symbol; failed-breakout silently skips |
| Tighter trailing re-attach fails after climax | logged as alert; climax_fired remains True; MA-trail and bracket stop still active |
| HALT file present | `submit_exit`, `submit_partial_exit`, `execute_plan`, `cancel_position_trailing` all check HALT and no-op cleanly; check_sepa_exits records skipped intents |
| `climax_check` insufficient data (e.g. <30 days OHLCV) | returns False; no climax; other rules proceed |
| pending_plan has Phase 1 partial AND failed-breakout fires | `_cancel_pending_partials(symbol)` removes the Phase 1 intent before `submit_exit` runs; only the full-exit intent reaches the broker |
| pending_plan has rebalancer's own buy intent for the same symbol | `_cancel_pending_partials` would remove that too; **mitigation**: filter cancellation to intents with `side=="sell"` and `reason starting "sepa-"` so we don't kill rebalance buys mid-day |

The last row is a real design consideration. `_cancel_pending_partials` will filter `s.intent.side == "sell" and s.intent.reason.startswith("sepa-")` so it never affects buy intents or non-SEPA exits.

---

## 7. Notifications

Each SEPA Phase 2 action appends one entry to `config.TELEGRAM_NOTIFY_PATH`, alongside Phase 1's existing notifications.

Failed-breakout:
```
⚠ SEPA failed-breakout — AAPL
Day 2 close $197.50 < entry pivot $200.50; full exit triggered.
Pending Phase 1 partial cancelled.
```

Climax:
```
🔥 SEPA climax — AAPL
Triple condition met:
  return +28% over 8 days
  ADR 2.4× of prior 20-day baseline
  volume 3.1× of prior 20-day baseline
Sold ~50% (≈$2,400) at $128.0; trailing tightened to 6%;
R-multiple scale-out disabled going forward.
```

---

## 8. Testing

### 8.1 `tests/test_sepa_exits.py` (extend)

- `test_failed_breakout_within_window_close_below_pivot_true`
- `test_failed_breakout_within_window_all_closes_above_pivot_false`
- `test_failed_breakout_window_expired_false`
- `test_failed_breakout_no_pivot_record_false`
- `test_failed_breakout_insufficient_closes_false`
- `test_climax_all_three_conditions_true`
- `test_climax_return_only_false`
- `test_climax_range_only_false`
- `test_climax_volume_only_false`
- `test_climax_insufficient_data_false`
- `test_climax_volume_recent_excluded_from_baseline` (volume baseline excludes the recent days under test)

### 8.2 `tests/test_watchdog.py` (extend)

- `test_check_sepa_exits_failed_breakout_full_exit_path`
- `test_check_sepa_exits_failed_breakout_cancels_pending_phase1_partial`
- `test_check_sepa_exits_failed_breakout_priority_over_r_multiple`
- `test_check_sepa_exits_failed_breakout_window_expired_skipped`
- `test_check_sepa_exits_climax_path_sells_half_and_tightens_trail`
- `test_check_sepa_exits_climax_sets_climax_fired_true`
- `test_check_sepa_exits_climax_disables_r_multiple_on_next_run`
- `test_check_sepa_exits_climax_allows_ma_trail_after_fired`
- `test_check_sepa_exits_climax_priority_over_r_multiple_same_day`
- `test_check_sepa_exits_gc_removes_exited_pivot_entries`
- `test_check_sepa_exits_cancel_pending_partials_filters_to_sepa_sells`

### 8.3 `tests/test_rebalancer.py` (extend)

- `test_rebalancer_writes_entry_pivots_for_screener_picks`
- `test_rebalancer_skips_entry_pivots_for_etf_entries`
- `test_rebalancer_overwrites_pivot_on_re_entry`

### 8.4 `tests/test_orders.py` (sync_state extend)

- `test_sync_state_initializes_climax_fired_false_on_first_seen_core_position`
- `test_sync_state_preserves_climax_fired_across_runs`
- `test_sync_state_does_not_append_r_tier_when_climax_fired_true`

### 8.5 `tests/test_screener.py` (extend)

- `test_screen_stocks_returns_base_hi_column`

---

## 9. Out of scope (recap)

- Stage-3 transition rule
- Aggressive-tranche SEPA application
- Quant-review override allowlist entries for SEPA Phase 2
- Climax detail persistence beyond the boolean flag (no climax_fired_at, no recorded condition values)
- Re-enabling R-multiple after climax fires
- Pivot detection for non-screener entries
- Multiple-pivot-per-symbol history (only latest entry's pivot is kept)
- Cross-position climax (sector-wide blow-off detection)

---

## 10. Open questions

None at spec time. Five questions during brainstorming — Phase 2 rule scope, pivot source, failed-breakout window/trigger, climax complexity, rule priority — were all resolved before this spec was written.
