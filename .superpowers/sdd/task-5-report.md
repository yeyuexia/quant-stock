# Task 5 Report: `data.fetch_estimates`

## Status: DONE

## Summary

Implemented `fetch_estimates(ticker: str) -> dict` in `quant/data/market.py` and added a fail-open test in `tests/test_data.py`, following TDD per the brief.

## TDD Steps

1. **Wrote failing test** — `test_fetch_estimates_failopen` added to `tests/test_data.py`. Monkeypatches `quant.data.market.yf.Ticker` to throw `RuntimeError("boom")`.
2. **Verified failure** — `AttributeError: module 'quant.data.market' has no attribute 'fetch_estimates'` (as expected).
3. **Implemented** — Added `_ESTIMATES_EMPTY` sentinel and `fetch_estimates` to `quant/data/market.py`, using existing `yf`, `_log`, `_run_with_timeout`, and `_TICKER_TIMEOUT`.
4. **Verified pass** — `test_fetch_estimates_failopen` passes; full `tests/test_data.py` suite: 21 passed.

## Files Changed

- `quant/data/market.py` — added `_ESTIMATES_EMPTY` dict and `fetch_estimates` function
- `tests/test_data.py` — added `test_fetch_estimates_failopen`

## Implementation Notes

- `fetch_estimates` is fully defensive: inner `try/except` blocks around both `eps_revisions` and `get_earnings_dates` calls handle yfinance version variance
- Outer `try/except` around `_run_with_timeout` catches timeout and any other error, returning `dict(_ESTIMATES_EMPTY)` (copy, not the sentinel itself)
- Did not touch `quant/agent/dossier.py`, `quant/agent/investor.py`, or `quant/config.py`
