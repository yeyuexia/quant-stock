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
from dataclasses import dataclass, field
from typing import Optional

import config
from broker import Broker, BrokerError
from pending_plan import PendingPlan, load_plan, write_plan, clear_plan

HALT_PATH = config.HALT_PATH


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

    try:
        if not broker.is_market_open():
            result.market_closed = True
            return result
    except BrokerError as e:
        result.notes.append(f"is_market_open error: {e}")
        return result

    result.shadow = bool(config.EXECUTOR_SHADOW_MODE)

    obs = _fetch_current_observations(plan, broker)

    _process_breakers(plan, obs, result)
    _process_slices(plan, obs, result, broker=broker)
    _process_eod(plan, result, broker=broker)
    _notify_breakers(result, plan)

    write_plan(plan)
    return result


def _process_breakers(plan: PendingPlan, obs, result: TickResult):
    """Evaluate all five breakers; update intent statuses + sticky list."""
    from breakers import (
        check_spy_drop, check_vix_spike, check_single_name_shock,
        check_news_shock, check_macro_flip,
    )

    already = set(plan.breakers_tripped)

    symbol_baselines = {
        s.intent.symbol: s.intent.decision_price
        for s in plan.intents
        if s.intent.decision_price is not None and s.intent.side == "buy"
    }

    evaluations = [
        check_spy_drop(plan.baseline, obs.spy),
        check_vix_spike(plan.baseline, obs.vix),
        check_news_shock(baseline=plan.baseline, hits=obs.news_hits,
                         spy_now=obs.spy, spy_15min_ago=obs.spy_15min_ago),
        check_macro_flip(plan.baseline, obs.macro),
    ]

    c_results = check_single_name_shock(plan.baseline, symbol_baselines, obs.symbol_prices)

    for r in evaluations:
        if not r.tripped:
            continue
        if r.breaker in already:
            continue
        already.add(r.breaker)
        result.tripped_breakers.append(r)
        _abort_for_breaker(plan, r, result)

    for r in c_results:
        if not r.tripped:
            continue
        if "C" not in already:
            already.add("C")
            result.tripped_breakers.append(r)
        for state in plan.intents:
            if state.status != "active":
                continue
            if state.intent.symbol in (r.affected_symbols or []):
                state.status = "aborted"
                state.abort_reason = f"C: {r.message}"
                result.aborted_intents.append(state.intent)

    plan.breakers_tripped = sorted(already)


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
    """Live implementation — stubbed in tests via monkeypatch."""
    from baseline import _fetch_spy, _fetch_vix, _fetch_macro_score
    from news_shock import fetch_recent_headlines, match_headlines, dedupe_by_title_hash

    spy_now = _fetch_spy()
    vix_now = _fetch_vix()
    macro_now = _fetch_macro_score()

    symbol_prices: dict = {}
    for state in plan.intents:
        try:
            bid, ask = broker.latest_quote(state.intent.symbol)
            symbol_prices[state.intent.symbol] = (bid + ask) / 2
        except BrokerError:
            continue

    headlines = fetch_recent_headlines(plan.baseline.news_cursor_at)
    plan_symbols = {s.intent.symbol for s in plan.intents}
    hits = match_headlines(headlines, config.NEWS_SHOCK_KEYWORDS, plan_symbols)
    hits = dedupe_by_title_hash(hits, config.CIRCUIT_BREAKERS["news_dedupe_minutes"])

    spy_15min_ago = _spy_15min_ago_price()

    return _Observations(
        spy=spy_now, vix=vix_now, macro=macro_now,
        symbol_prices=symbol_prices,
        spy_15min_ago=spy_15min_ago, news_hits=hits,
    )


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
    """US/Eastern wall clock. Monkeypatched in tests."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        return dt.datetime.utcnow() - dt.timedelta(hours=4)


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


def _process_slices(plan: PendingPlan, obs, result: TickResult, *, broker):
    """For each active intent, cancel prior unfilled limit, then submit
    the next slice if its window has passed and max_price is respected."""
    from orders import submit_limit_slice
    from dataclasses import replace
    now = _now_et()

    for state in plan.intents:
        if state.status != "active":
            continue
        intent = state.intent
        if intent.slice_count is None:
            result.notes.append(f"{intent.symbol}: no slice_count, skipping")
            continue

        if state.last_client_order_id:
            prior_filled = _broker_filled_notional(broker, state.last_client_order_id)
            state.notional_filled += prior_filled
            _cancel_prior(broker, state.last_client_order_id, result)
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
            bid, ask = broker.latest_quote(intent.symbol)
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


def _cancel_prior(broker, client_order_id: str, result: TickResult):
    """Cancel the open order whose client_order_id matches. Uses get_open_orders
    to resolve the broker order ID, then cancels by that ID."""
    try:
        open_orders = broker.get_open_orders()
        matched = next(
            (o for o in open_orders if o.client_order_id == client_order_id), None
        )
        if matched is None:
            # Already filled or already canceled — nothing to do.
            return
        broker.cancel_order(matched.id)
        result.canceled.append(matched.id)
    except BrokerError as e:
        result.notes.append(f"cancel_order({client_order_id}) failed: {e}")


def _broker_filled_notional(broker, client_order_id: str) -> float:
    """Safely query the broker for a prior order's filled notional."""
    try:
        return float(broker.get_filled_notional(client_order_id))
    except Exception:
        return 0.0


def _notify_breakers(result: TickResult, plan: PendingPlan):
    """Append one notification per tripped breaker to the Telegram notify file."""
    import json
    if not result.tripped_breakers:
        return
    path = getattr(config, "TELEGRAM_NOTIFY_PATH", None)
    if not path:
        return

    existing = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception:
            existing = []

    for r in result.tripped_breakers:
        existing.append({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "plan_id": plan.plan_id,
            "breaker": r.breaker,
            "scope": r.scope,
            "message": r.message,
            "aborted": [i.symbol for i in result.aborted_intents],
        })

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


def _process_eod(plan: PendingPlan, result: TickResult, *, broker):
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
            state.notional_filled += _broker_filled_notional(broker, state.last_client_order_id)
            _cancel_prior(broker, state.last_client_order_id, result)
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
