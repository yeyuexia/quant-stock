"""Intraday execution tick handler.

Cron entry: `python3 executor.py` — fires every 10 min during market hours.
Stateless per tick: all durable state lives in .cache/pending_plan.json.

This module handles the plumbing (read plan, rails, fetch observations,
orchestrate breakers, submit slices, cleanup). The five breakers live in
breakers.py; slice submission lives in orders.py; signal fetching lives in
baseline.py and news_shock.py.
"""
from __future__ import annotations
import datetime as dt
import os
from quant import paths
from dataclasses import dataclass, field
from typing import Optional

import quant.config as config
from quant.execution.broker import Broker, BrokerError
from quant.execution.pending_plan import PendingPlan, load_plan, write_plan, clear_plan

HALT_PATH = config.HALT_PATH

# E11: simple 1-retry wrapper for broker calls that can fail on transient
# Alpaca blips. Each broker call site uses this so a single network hiccup
# doesn't poison the tick (executor can't "try again tomorrow" — a missed
# slice window is gone).
_BROKER_RETRY_BACKOFF_MS = 300


def _retry_broker(fn, *args, attempts: int = 2, **kwargs):
    """Call fn() with up to `attempts` total tries; brief sleep between.
    Re-raises the last exception if all attempts fail."""
    import time as _time
    last_exc = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except BrokerError as e:
            last_exc = e
            if i < attempts - 1:
                _time.sleep(_BROKER_RETRY_BACKOFF_MS / 1000.0)
    raise last_exc  # type: ignore[misc]


@dataclass
class TickResult:
    halted: bool = False
    market_closed: bool = False
    no_plan: bool = False
    shadow: bool = False
    submitted: list = field(default_factory=list)
    would_submit: list = field(default_factory=list)
    canceled: list = field(default_factory=list)
    tripped_breakers: list = field(default_factory=list)
    aborted_intents: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def run_tick(*, broker) -> Optional[TickResult]:
    """Execute one 10-minute tick. Returns None if no plan exists."""
    result = TickResult()

    plan = load_plan()
    if plan is None:
        result.no_plan = True
        return None

    if os.path.exists(HALT_PATH):
        result.halted = True
        return result

    # E8: outside RTH per local clock → definitely closed; skip the broker API
    # call (saves ~150ms × 36 ticks/day on the broker even when there's no
    # work). On weekday holidays the local clock can't tell, but Alpaca's
    # submit_limit_slice / latest_quote will fail naturally and result.notes
    # will reflect it.
    if not _is_rth_now():
        result.market_closed = True
        return result

    result.shadow = bool(config.EXECUTOR_SHADOW_MODE)

    # E10: snapshot plan state BEFORE observations so we catch news_hits_seen
    # mutations too (observations dedup against and update that dict).
    plan_hash_before = _plan_fingerprint(plan)

    obs = _fetch_current_observations(plan, broker)

    # E4: cache get_open_orders for the whole tick — saves N broker round-trips
    # across _process_slices + _process_eod (one per active intent otherwise).
    open_orders_by_cid = _snapshot_open_orders(broker, result)

    _process_breakers(plan, obs, result)
    _process_slices(plan, obs, result, broker=broker, open_orders=open_orders_by_cid)
    _process_eod(plan, result, broker=broker, open_orders=open_orders_by_cid)
    _notify_breakers(result, plan)

    if _plan_fingerprint(plan) != plan_hash_before:
        write_plan(plan)
    return result


def _plan_fingerprint(plan: PendingPlan) -> str:
    """Stable hash of all PendingPlan fields the executor can mutate.
    Used by run_tick to skip write_plan when nothing changed this tick."""
    import hashlib
    from quant.execution.pending_plan import _plan_to_dict
    import json as _json
    blob = _json.dumps(_plan_to_dict(plan), sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def _snapshot_open_orders(broker, result: TickResult) -> dict:
    """One-shot open-orders fetch, keyed by client_order_id. Failure → {}."""
    try:
        orders_list = _retry_broker(broker.get_open_orders)
        return {o.client_order_id: o for o in orders_list}
    except BrokerError as e:
        result.notes.append(f"get_open_orders error: {e}")
        return {}


def _process_breakers(plan: PendingPlan, obs, result: TickResult):
    """Evaluate breakers + propagate aborts.

    Two phases:
      1. Re-apply sticky aborts to active intents — catches mid-day merges
         added by a manual rebalancer run AFTER a breaker tripped (E5).
      2. Evaluate only the breakers that haven't already tripped (E1) and
         apply their aborts on first trip.
    """
    from quant.execution.breakers import (
        check_spy_drop, check_vix_spike, check_single_name_shock,
        check_news_shock, check_macro_flip,
    )

    already = set(plan.breakers_tripped)

    # Phase 1 — sticky abort re-application. Cheap: just walks intents.
    _apply_sticky_aborts(plan, already, result)

    symbol_baselines = {
        s.intent.symbol: s.intent.decision_price
        for s in plan.intents
        if s.intent.decision_price is not None and s.intent.side == "buy"
        and s.status == "active"
    }

    # Phase 2 — only evaluate breakers that haven't already tripped (saves the
    # SPY/VIX/macro/news fetches when they're irrelevant for the rest of the day).
    if "A" not in already:
        r = check_spy_drop(plan.baseline, obs.spy)
        if r.tripped:
            already.add("A")
            result.tripped_breakers.append(r)
            _abort_for_breaker(plan, r, result)
    if "B" not in already:
        r = check_vix_spike(plan.baseline, obs.vix)
        if r.tripped:
            already.add("B")
            result.tripped_breakers.append(r)
            _abort_for_breaker(plan, r, result)
    if "D" not in already:
        r = check_news_shock(baseline=plan.baseline, hits=obs.news_hits,
                             spy_now=obs.spy, spy_15min_ago=obs.spy_15min_ago)
        # Audit log: write every hit regardless of corroboration. This used
        # to live INSIDE check_news_shock — moved out so the breaker can
        # stay pure (no disk I/O) and tests can call it without fixtures.
        if obs.news_hits:
            from quant.signals.news_shock import log_hit
            for h in obs.news_hits:
                try:
                    log_hit(h, corroborated=r.tripped)
                except Exception:
                    pass  # audit log failure must not poison the tick
        if r.tripped:
            already.add("D")
            result.tripped_breakers.append(r)
            _abort_for_breaker(plan, r, result)
    if "E" not in already:
        r = check_macro_flip(plan.baseline, obs.macro)
        if r.tripped:
            already.add("E")
            result.tripped_breakers.append(r)
            _abort_for_breaker(plan, r, result)

    # C is per-symbol — only evaluate symbols not already C-tripped.
    pending_c = {
        sym: base for sym, base in symbol_baselines.items()
        if f"C:{sym}" not in already
    }
    c_results = check_single_name_shock(plan.baseline, pending_c, obs.symbol_prices)
    for r in c_results:
        if not r.tripped:
            continue
        sym = (r.affected_symbols or [None])[0]
        key = f"C:{sym}"
        already.add(key)
        result.tripped_breakers.append(r)
        for state in plan.intents:
            if state.status != "active":
                continue
            if state.intent.symbol in (r.affected_symbols or []):
                state.status = "aborted"
                state.abort_reason = f"C: {r.message}"
                result.aborted_intents.append(state.intent)

    plan.breakers_tripped = sorted(already)


# Scope mapping for sticky abort re-application.
_BROAD_BREAKER_SCOPE = {
    "A": "buys",          # SPY drop
    "B": "buys",          # VIX spike
    "D": "buys",          # news shock
    "E": "risk_on_buys",  # macro flip (defensive symbols exempt)
}


def _apply_sticky_aborts(plan: PendingPlan, already: set, result: TickResult):
    """Re-apply sticky aborts to active intents.

    Necessary because between ticks, _write_pending_plan may have merged new
    intents from another tranche's rebalance — those would otherwise execute
    despite a breaker still being tripped from earlier in the day.
    """
    for key in already:
        if key.startswith("C:"):
            sym = key[2:]
            for state in plan.intents:
                if state.status != "active":
                    continue
                if state.intent.symbol == sym:
                    state.status = "aborted"
                    state.abort_reason = f"C: sticky (rank {key})"
                    result.aborted_intents.append(state.intent)
            continue
        scope = _BROAD_BREAKER_SCOPE.get(key)
        if scope is None:
            continue
        for state in plan.intents:
            if state.status != "active":
                continue
            i = state.intent
            if scope == "buys" and i.side != "buy":
                continue
            if scope == "risk_on_buys":
                if i.side != "buy":
                    continue
                if i.symbol in config.DEFENSIVE_SYMBOLS:
                    continue
            state.status = "aborted"
            state.abort_reason = f"{key}: sticky"
            result.aborted_intents.append(i)


def _abort_for_breaker(plan: PendingPlan, r, result: TickResult):
    """Apply a broad-scope abort (A/B/D/E)."""
    for state in plan.intents:
        if state.status != "active":
            continue
        i = state.intent
        if r.scope == "buys" and i.side != "buy":
            continue
        if r.scope == "risk_on_buys":
            if i.side != "buy":
                continue
            if i.symbol in config.DEFENSIVE_SYMBOLS:
                continue
        state.status = "aborted"
        state.abort_reason = f"{r.breaker}: {r.message}"
        result.aborted_intents.append(i)


def _fetch_current_observations(plan: PendingPlan, broker):
    """Live implementation — stubbed in tests via monkeypatch.

    Optimizations:
      - E3: only quote symbols with status="active" (skip aborted/done).
      - E4: a single broker.get_open_orders() call is cached on the result so
            _process_slices can reuse it without N more round-trips.
      - E2: SPY 15-min-ago is only fetched when news hits exist (its only
            consumer is breaker D's corroboration test).
      - E6: news hits are deduped against plan.news_hits_seen (cross-tick state)
            so the audit log doesn't get the same headline 30 times.
    """
    from quant.signals.baseline import _fetch_spy, _fetch_vix, _fetch_macro_score
    from quant.signals.news_shock import (
        fetch_recent_headlines, match_headlines, dedupe_by_title_hash, title_hash,
    )

    spy_now = _fetch_spy()
    vix_now = _fetch_vix()
    macro_now = _fetch_macro_score()

    symbol_prices: dict = {}
    for state in plan.intents:
        if state.status != "active":
            continue
        try:
            bid, ask = _retry_broker(broker.latest_quote, state.intent.symbol)
            symbol_prices[state.intent.symbol] = (bid + ask) / 2
        except BrokerError:
            continue

    # If D already tripped, skip news fetch entirely — saves two HTTP calls.
    already = set(plan.breakers_tripped)
    if "D" in already:
        hits = []
        spy_15min_ago = 0.0
    else:
        headlines = fetch_recent_headlines(plan.baseline.news_cursor_at)
        plan_symbols = {s.intent.symbol for s in plan.intents if s.status == "active"}
        hits = match_headlines(headlines, config.NEWS_SHOCK_KEYWORDS, plan_symbols)
        hits = dedupe_by_title_hash(hits, config.CIRCUIT_BREAKERS["news_dedupe_minutes"])

        # E6: drop hits we've already observed and logged on prior ticks.
        dedupe_window_min = config.CIRCUIT_BREAKERS["news_dedupe_minutes"]
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=dedupe_window_min)
        # Prune stale entries so the dict doesn't grow forever.
        plan.news_hits_seen = {
            h: ts for h, ts in plan.news_hits_seen.items()
            if _parse_iso(ts) >= cutoff
        }
        fresh_hits = []
        for hit in hits:
            h = title_hash(hit.title)
            if h in plan.news_hits_seen:
                continue
            fresh_hits.append(hit)
            plan.news_hits_seen[h] = hit.ts.isoformat()
        hits = fresh_hits

        # E2: only fetch the expensive 15min-ago bar when we have hits to corroborate.
        spy_15min_ago = _spy_15min_ago_price() if hits else 0.0

    return _Observations(
        spy=spy_now, vix=vix_now, macro=macro_now,
        symbol_prices=symbol_prices,
        spy_15min_ago=spy_15min_ago, news_hits=hits,
    )


def _parse_iso(ts) -> dt.datetime:
    """Tolerant ISO parse used by news_hits_seen pruning."""
    if isinstance(ts, dt.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(ts))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


@dataclass
class _Observations:
    spy: float
    vix: float
    macro: float
    symbol_prices: dict
    spy_15min_ago: float
    news_hits: list


def _spy_15min_ago_price() -> float:
    """Fetch SPY close ~15 min ago via yfinance 1m bars."""
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(period="1d", interval="1m")
        if h.empty:
            return 0.0
        target_idx = -16 if len(h) >= 16 else 0
        return float(h["Close"].iloc[target_idx])
    except Exception:
        return 0.0


def _now_et() -> dt.datetime:
    """US/Eastern wall clock. Monkeypatched in tests.
    Delegates to timeutils.now_et — see there for environment requirements.
    """
    from quant.infra.timeutils import now_et
    return now_et()


def _is_rth_now() -> bool:
    """Local-clock RTH gate. Calls _now_et() (not timeutils.is_rth_now) so
    test monkeypatching of executor._now_et propagates to this check."""
    now = _now_et()
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now < close_t


def _slice_windows(slice_count: int) -> list:
    """Time-of-day anchors for each slice. ET, naive."""
    if slice_count <= 1:
        return [dt.time(10, 0)]
    start_minutes = 10 * 60 + 30     # 10:30
    end_minutes   = 14 * 60 + 30     # 14:30
    span = end_minutes - start_minutes
    step = span // (slice_count - 1)
    mins_list = [start_minutes + i * step for i in range(slice_count)]
    return [dt.time(m // 60, m % 60) for m in mins_list]


def _next_slice_due(
    *, now: dt.datetime, windows: list, slices_submitted: int,
) -> Optional[int]:
    """Return the index of the next slice that is due. None if nothing due
    or all already submitted."""
    if slices_submitted >= len(windows):
        return None
    next_idx = slices_submitted
    window_dt = dt.datetime.combine(now.date(), windows[next_idx])
    if now >= window_dt:
        return next_idx
    return None


def _process_slices(plan: PendingPlan, obs, result: TickResult, *, broker,
                    open_orders: Optional[dict] = None):
    """For each active intent, cancel prior unfilled limit, then submit
    the next slice if its window has passed and max_price is respected.

    Bails early at/after EXECUTOR_WINDOW_END so we don't submit slices that
    _process_eod would immediately cancel on the same tick.
    """
    from quant.execution.orders import submit_limit_slice
    from dataclasses import replace
    now = _now_et()

    end_h, end_m = map(int, config.EXECUTOR_WINDOW_END.split(":"))
    eod = dt.datetime.combine(now.date(), dt.time(end_h, end_m))
    if now >= eod:
        return

    for state in plan.intents:
        if state.status != "active":
            continue
        intent = state.intent
        if intent.slice_count is None:
            result.notes.append(f"{intent.symbol}: no slice_count, skipping")
            continue

        if state.last_client_order_id:
            prior_filled = _broker_filled_notional(broker, state.last_client_order_id)
            if prior_filled is None:
                # Fill state unknown (transient broker query error). Skip
                # cancel + don't advance state. Next tick will retry the
                # query; double-submitting on top of an unobserved partial
                # fill is the worse failure mode.
                result.notes.append(
                    f"{intent.symbol}: fill query returned None, "
                    f"deferring cancel+resubmit this tick"
                )
                continue
            state.notional_filled += prior_filled
            if prior_filled > 0:
                _notify_fill_to_telegram(state.intent, prior_filled, state.notional_filled)
            _cancel_prior(broker, state.last_client_order_id, result,
                          open_orders=open_orders)
            state.last_client_order_id = None

        if state.notional_filled >= intent.notional * 0.95:
            state.status = "done"
            continue

        windows = _slice_windows(intent.slice_count)
        next_idx = _next_slice_due(
            now=now, windows=windows, slices_submitted=state.slices_submitted,
        )
        if next_idx is None:
            continue

        remaining_slices = intent.slice_count - state.slices_submitted
        slice_size = max(1.0, round(
            (intent.notional - state.notional_filled) / remaining_slices, 2,
        ))

        try:
            bid, ask = _retry_broker(broker.latest_quote, intent.symbol)
        except BrokerError as e:
            result.notes.append(f"{intent.symbol}: quote error {e}")
            continue

        if intent.side == "buy":
            if intent.max_price is not None and ask > intent.max_price:
                result.notes.append(
                    f"{intent.symbol}: ask {ask:.4f} > max_price "
                    f"{intent.max_price:.4f} — slice skipped, will retry next tick"
                )
                continue
            ceiling = intent.max_price if intent.max_price is not None else ask * 1.001
            limit_price = round(min(ask * 1.001, ceiling), 2)
        else:
            if intent.max_price is not None and bid < intent.max_price:
                result.notes.append(
                    f"{intent.symbol}: bid {bid:.4f} < min_price "
                    f"{intent.max_price:.4f} — slice skipped, will retry next tick"
                )
                continue
            floor = intent.max_price if intent.max_price is not None else bid * 0.999
            limit_price = round(max(bid * 0.999, floor), 2)

        if config.EXECUTOR_SHADOW_MODE:
            result.would_submit.append({
                "symbol": intent.symbol,
                "side": intent.side,
                "slice_size": slice_size,
                "limit_price": limit_price,
            })
            continue

        slice_cid = f"{intent.client_order_id}-s{state.slices_submitted + 1}"
        slice_intent = replace(intent, client_order_id=slice_cid)
        slice_result = submit_limit_slice(
            slice_intent, limit_price=limit_price,
            notional=slice_size, broker=broker,
        )
        if slice_result.submitted:
            o = slice_result.submitted[0]
            result.submitted.append(o)
            state.slices_submitted += 1
            state.last_client_order_id = o.client_order_id
            state.last_limit_price = limit_price
        elif slice_result.deferred:
            result.deferred.extend(slice_result.deferred)
        elif slice_result.queued:
            result.notes.append(
                f"{intent.symbol}: slice queued for Telegram approval"
            )
        elif slice_result.skipped:
            for _, msg in slice_result.skipped:
                result.notes.append(f"{intent.symbol}: skipped ({msg})")


def _cancel_prior(broker, client_order_id: str, result: TickResult,
                  *, open_orders: Optional[dict] = None):
    """Cancel the open order whose client_order_id matches.

    Looks up the broker order ID from the per-tick open_orders cache if
    provided; falls back to a fresh broker.get_open_orders() call if not
    (preserves the old call shape for ad-hoc callers).
    """
    try:
        if open_orders is None:
            open_orders = {o.client_order_id: o for o in broker.get_open_orders()}
        matched = open_orders.get(client_order_id)
        if matched is None:
            # Already filled or already canceled — nothing to do.
            return
        broker.cancel_order(matched.id)
        result.canceled.append(matched.id)
    except BrokerError as e:
        result.notes.append(f"cancel_order({client_order_id}) failed: {e}")


def _broker_filled_notional(broker, client_order_id: str) -> Optional[float]:
    """Safely query the broker for a prior order's filled notional.

    Returns:
      - float ≥ 0 on success (0.0 = order exists but not filled)
      - None on query failure (caller should NOT proceed with cancel+resubmit
        on top of an unobserved partial fill — we'd risk double-submitting)
    """
    try:
        val = _retry_broker(broker.get_filled_notional, client_order_id)
    except Exception:
        return None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _notify_breakers(result: TickResult, plan: PendingPlan):
    """Append one notification per tripped breaker to the Telegram notify file."""
    if not result.tripped_breakers:
        return
    from quant.infra.notifications import append_notification
    for r in result.tripped_breakers:
        append_notification({
            "source": "executor.breaker",
            "plan_id": plan.plan_id,
            "breaker": r.breaker,
            "scope": r.scope,
            "message": r.message,
            "aborted": [i.symbol for i in result.aborted_intents],
        })


def _notify_fill_to_telegram(intent, filled_delta: float, total_filled: float) -> None:
    """Append a fill notification to TELEGRAM_NOTIFY_PATH. No-op if unset."""
    if filled_delta <= 0:
        return
    from quant.infra.notifications import append_notification

    pct = (total_filled / intent.notional * 100) if intent.notional else 0.0
    message = "\n".join([
        f"✅ Order Filled — {intent.symbol}",
        f"{intent.side.upper()} ${filled_delta:,.0f}  "
        f"({pct:.1f}% of ${intent.notional:,.0f})",
        f"tranche={intent.tranche}  tier={intent.tier}",
    ])
    append_notification({
        "source": "executor-fill",
        "symbol": intent.symbol,
        "side": intent.side,
        "filled_notional": filled_delta,
        "total_filled": total_filled,
        "intent_notional": intent.notional,
        "message": message,
    })


def _process_eod(plan: PendingPlan, result: TickResult, *, broker,
                 open_orders: Optional[dict] = None):
    """At the last tick of the day, cancel all outstanding limits and mark
    each intent done/deferred based on fill ratio."""
    now = _now_et()
    end_h, end_m = map(int, config.EXECUTOR_WINDOW_END.split(":"))
    eod = dt.datetime.combine(now.date(), dt.time(end_h, end_m))
    if now < eod:
        return

    for state in plan.intents:
        if state.status != "active":
            continue
        if state.last_client_order_id:
            prior_filled = _broker_filled_notional(broker, state.last_client_order_id)
            if prior_filled is None:
                # Fill state unknown at EOD — skip cancel; tomorrow's run
                # will re-observe. Better than canceling an order we might
                # actually have filled.
                result.notes.append(
                    f"{state.intent.symbol}: EOD fill query returned None, "
                    f"leaving order outstanding"
                )
                continue
            state.notional_filled += prior_filled
            if prior_filled > 0:
                _notify_fill_to_telegram(state.intent, prior_filled, state.notional_filled)
            _cancel_prior(broker, state.last_client_order_id, result,
                          open_orders=open_orders)
            state.last_client_order_id = None
        intent = state.intent
        fill_ratio = state.notional_filled / max(1.0, intent.notional)
        if fill_ratio >= 0.95:
            state.status = "done"
        else:
            state.status = "deferred"
            state.abort_reason = (
                f"EOD deferred at {fill_ratio * 100:.1f}% filled"
            )
            result.deferred.append(intent)


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(paths.REPO_ROOT, ".env"))
    except ImportError:
        pass
    broker = Broker(env=config.ALPACA_ENV)
    result = run_tick(broker=broker)
    if result is None:
        print("executor: no pending plan — exiting")
        return
    if result.halted:
        print("executor: HALT file present — exiting")
        return
    if result.market_closed:
        print("executor: market closed — exiting")
        return
    print(f"executor: tick complete "
          f"(submitted={len(result.submitted)} would_submit={len(result.would_submit)} "
          f"tripped={result.tripped_breakers})")


if __name__ == "__main__":
    main()
