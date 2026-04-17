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
    # Task 16+17 add slice scheduling & submission after this line.
    # Task 18 adds end-of-day cleanup.

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
                if state.intent.symbol in config.DEFENSIVE_SYMBOLS:
                    continue
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
