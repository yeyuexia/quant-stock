# Position Adoption + Synthetic Enforced Stops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the rebalancer manage broker-imported positions (unfreeze the book) and enforce stop-losses that work on fractional shares, so neither failure can silently recur.

**Architecture:** Auto-adopt untagged positions into a sleeve at the single reconciliation chokepoint (`orders.sync_state`); add a starvation guardrail alert there; and replace the watchdog's alert-only stop check with an active market-sell that works on fractional quantities. All three behaviors are gated by config flags.

**Tech Stack:** Python, pytest, Alpaca (`alpaca-py`), pandas. Existing modules: `orders.py`, `watchdog.py`, `config.py`, `rebalancer.py`. Test fakes in `tests/fakes.py` (`FakeBroker`).

## Global Constraints

- Python; tests via `pytest`. Run a single test with `pytest tests/<file>::<name> -v`.
- Tests are state-isolated by the autouse `_isolate_persistent_state` fixture in `tests/conftest.py` (redirects `portfolio.json` / logs to tmp). Do not write real state files in tests.
- `FakeBroker` (`tests/fakes.py`): `seed_position(symbol, qty, avg_entry, mv=None)`, fields `cash`/`equity` settable directly, `set_latest_price(sym, price)`, records submitted orders in `_submitted` (each has `.side`, `.symbol`, `.type`). Duplicate `client_order_id` raises `BrokerError("duplicate ...")`.
- Sleeve classification rule (verbatim): `aggressive` if `symbol in config.ETF_LEVERAGED`, else `core`.
- Config defaults (verbatim): `ADOPT_EXTERNAL_POSITIONS = True`, `UNKNOWN_MV_HALT_PCT = 0.20`, `ENFORCE_STOPS = True`.
- Per standing user instruction: update `README.md` before reporting the work complete (Task 6).
- Commit after every task.

---

### Task 1: Config flags

**Files:**
- Modify: `config.py` (add three module-level constants near the other derived params, after line ~94 where `USE_LEVERAGED_ETFS` is defined)
- Test: `tests/test_config_flags.py` (create)

**Interfaces:**
- Produces: `config.ADOPT_EXTERNAL_POSITIONS: bool`, `config.UNKNOWN_MV_HALT_PCT: float`, `config.ENFORCE_STOPS: bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_flags.py`:

```python
import config


def test_new_flags_have_expected_defaults():
    assert config.ADOPT_EXTERNAL_POSITIONS is True
    assert config.UNKNOWN_MV_HALT_PCT == 0.20
    assert config.ENFORCE_STOPS is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_flags.py -v`
Expected: FAIL with `AttributeError: module 'config' has no attribute 'ADOPT_EXTERNAL_POSITIONS'`

- [ ] **Step 3: Add the constants**

In `config.py`, immediately after the line `USE_LEVERAGED_ETFS = _params["use_leveraged_etfs"]` (~line 94), add:

```python

# ── Position-adoption & stop-enforcement flags ──────────────────
# Broker-imported positions (manual trades, legacy holdings) arrive with no
# local metadata. When True, sync_state tags them into a sleeve so the
# rebalancer manages them and stop logic applies. When False, they stay
# 'unknown' (legacy behavior).
ADOPT_EXTERNAL_POSITIONS = True

# Defense-in-depth: if untagged ('unknown') market value exceeds this fraction
# of equity, sync_state raises a loud alert — the rebalancer would otherwise
# silently size itself to near-zero capital.
UNKNOWN_MV_HALT_PCT = 0.20

# When True, the intraday watchdog submits a market sell on a stop/trailing
# breach (works on fractional shares, unlike native stop orders). When False,
# the watchdog only alerts (legacy behavior).
ENFORCE_STOPS = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_flags.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config_flags.py
git commit -m "feat(config): add adoption + stop-enforcement flags"
```

---

### Task 2: Auto-adopt external positions in sync_state

**Files:**
- Modify: `orders.py:209-217` (the `if meta is None:` branch inside `sync_state`)
- Modify: `tests/test_orders.py:82-94` (existing `test_sync_state_marks_unknown_tranche` — adoption is now default-on, so it must opt out of adoption to still see "unknown")
- Test: `tests/test_orders.py` (add two new tests)

**Interfaces:**
- Consumes: `config.ADOPT_EXTERNAL_POSITIONS`, `config.ETF_LEVERAGED` (Task 1 + existing)
- Produces: positions with no prior metadata get `tranche ∈ {"core","aggressive"}` and `entry_reason == "adopted"` when adoption is on.

- [ ] **Step 1: Update the existing unknown-tranche test to opt out of adoption**

Replace `tests/test_orders.py:82-94` (`test_sync_state_marks_unknown_tranche`) body so it disables adoption:

```python
def test_sync_state_marks_unknown_tranche(tmp_path, monkeypatch):
    from orders import sync_state
    monkeypatch.setattr("config.ADOPT_EXTERNAL_POSITIONS", False)

    _portfolio_cache(tmp_path, monkeypatch, None)  # no cache

    fb = FakeBroker()
    fb.seed_position("NVDA", qty=5, avg_entry=100, mv=520)

    alerts: list = []
    snap = sync_state(fb, alerts=alerts)

    assert snap.positions[0]["tranche"] == "unknown"
    assert any("unknown" in a.lower() and "NVDA" in a for a in alerts)
```

- [ ] **Step 2: Write the failing tests for adoption**

Add to `tests/test_orders.py` (after `test_sync_state_marks_unknown_tranche`):

```python
def test_sync_state_adopts_external_position_into_core(tmp_path, monkeypatch):
    from orders import sync_state
    monkeypatch.setattr("config.ADOPT_EXTERNAL_POSITIONS", True)

    _portfolio_cache(tmp_path, monkeypatch, None)  # no cache → external

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=5, avg_entry=100, mv=520)

    alerts: list = []
    snap = sync_state(fb, alerts=alerts)

    assert snap.positions[0]["tranche"] == "core"
    assert snap.positions[0]["entry_reason"] == "adopted"
    assert any("adopted" in a.lower() and "AAPL" in a for a in alerts)


def test_sync_state_adopts_leveraged_etf_into_aggressive(tmp_path, monkeypatch):
    from orders import sync_state
    monkeypatch.setattr("config.ADOPT_EXTERNAL_POSITIONS", True)

    _portfolio_cache(tmp_path, monkeypatch, None)

    fb = FakeBroker()
    fb.seed_position("SOXL", qty=5, avg_entry=100, mv=520)  # in config.ETF_LEVERAGED

    snap = sync_state(fb, alerts=[])
    assert snap.positions[0]["tranche"] == "aggressive"
    assert snap.positions[0]["entry_reason"] == "adopted"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_orders.py -k "adopts_external or adopts_leveraged" -v`
Expected: FAIL — adopted positions currently come back as `tranche == "unknown"`.

- [ ] **Step 4: Implement adoption**

In `orders.py`, replace the `if meta is None:` branch (currently lines 210-214):

```python
        meta = old_meta.get(p.symbol)
        if meta is None:
            alerts.append(f"Unknown position on Alpaca: {p.symbol} ({p.qty} sh). "
                          f"Tag with orders.tag_position('{p.symbol}', 'core'|'aggressive').")
            tranche = "unknown"
            entry_reason = "external"
        else:
```

with:

```python
        meta = old_meta.get(p.symbol)
        if meta is None:
            if config.ADOPT_EXTERNAL_POSITIONS:
                tranche = "aggressive" if p.symbol in config.ETF_LEVERAGED else "core"
                entry_reason = "adopted"
                alerts.append(f"Adopted external position {p.symbol} ({p.qty} sh) "
                              f"into {tranche} sleeve.")
            else:
                alerts.append(f"Unknown position on Alpaca: {p.symbol} ({p.qty} sh). "
                              f"Tag with orders.tag_position('{p.symbol}', 'core'|'aggressive').")
                tranche = "unknown"
                entry_reason = "external"
        else:
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_orders.py -k "adopts_external or adopts_leveraged or marks_unknown" -v`
Expected: PASS (all three).

- [ ] **Step 6: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "feat(orders): auto-adopt external positions into a sleeve in sync_state"
```

---

### Task 3: Starvation guardrail alert in sync_state

**Files:**
- Modify: `orders.py` — add a check after the position-building loop (after current line 294, before the "Emit closed events" loop at line 296), inside `sync_state`
- Test: `tests/test_orders.py` (add one test)

**Interfaces:**
- Consumes: `config.UNKNOWN_MV_HALT_PCT`, the `alerts` list param, `acc.equity`, the assembled `positions` list.
- Produces: an alert string containing `"capital starved"` when untagged MV exceeds the threshold.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orders.py`:

```python
def test_sync_state_alerts_when_untagged_starves_rebalancer(tmp_path, monkeypatch):
    from orders import sync_state
    monkeypatch.setattr("config.ADOPT_EXTERNAL_POSITIONS", False)  # keep them unknown
    monkeypatch.setattr("config.UNKNOWN_MV_HALT_PCT", 0.20)

    _portfolio_cache(tmp_path, monkeypatch, None)

    fb = FakeBroker()
    fb.equity = 100_000.0
    fb.seed_position("NVDA", qty=100, avg_entry=500, mv=90_000)  # 90% of equity, untagged

    alerts: list = []
    sync_state(fb, alerts=alerts)

    assert any("capital starved" in a.lower() for a in alerts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_orders.py::test_sync_state_alerts_when_untagged_starves_rebalancer -v`
Expected: FAIL — no such alert is produced.

- [ ] **Step 3: Implement the guardrail**

In `orders.py`, immediately after the `for p in live:` loop that appends to `positions` (i.e. right after current line 294 `})` that closes the `positions.append({...})`, and before the comment `# Emit "closed" events ...` at line 296), insert:

```python
    # ── Starvation guardrail ────────────────────────────────────
    # Untagged ('unknown') positions are excluded from the rebalancer's
    # addressable capital (rebalancer._system_equity). If they dominate the
    # book, the rebalancer silently sizes to near-zero capital and stops
    # trading — exactly the failure this guards against.
    unknown_mv = sum(
        float(p.get("market_value", 0) or 0)
        for p in positions if p.get("tranche") == "unknown"
    )
    if acc.equity and unknown_mv / float(acc.equity) > config.UNKNOWN_MV_HALT_PCT:
        pct = 100.0 * unknown_mv / float(acc.equity)
        alerts.append(
            f"CRITICAL: untagged positions are {pct:.0f}% of equity — "
            f"rebalancer capital starved. Tag them or set "
            f"config.ADOPT_EXTERNAL_POSITIONS = True."
        )

```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_orders.py::test_sync_state_alerts_when_untagged_starves_rebalancer -v`
Expected: PASS

- [ ] **Step 5: Run the full sync_state suite to confirm no regressions**

Run: `pytest tests/test_orders.py -k sync_state -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "feat(orders): alert when untagged positions starve the rebalancer"
```

---

### Task 4: Synthetic enforced stop in watchdog

**Files:**
- Modify: `watchdog.py:25` (import `BrokerError`) and `watchdog.py:263-277` (the stop/trailing-stop check inside `check_price_moves`)
- Test: `tests/test_watchdog.py` (add two tests)

**Interfaces:**
- Consumes: `config.ENFORCE_STOPS`, `config.HALT_PATH`, `broker.submit_market`, `orders._make_cid`, `orders._append_daily_log`. The `broker` param of `check_price_moves` (None in the daily run, set in the intraday run).
- Produces: on a breach, a market SELL order for the full position qty + a `STOP ENFORCED` CRITICAL alert; legacy alert-only path preserved when `broker is None` or `ENFORCE_STOPS` is False.

- [ ] **Step 1: Add the BrokerError import**

In `watchdog.py`, change line 25 from:

```python
from broker import Broker
```

to:

```python
from broker import Broker, BrokerError
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_watchdog.py`:

```python
def test_check_price_moves_enforces_stop_with_market_sell(tmp_path, monkeypatch):
    import watchdog
    import pandas as pd
    from tests.fakes import FakeBroker

    monkeypatch.setattr("config.ENFORCE_STOPS", True)
    # No HALT file: point HALT_PATH at a non-existent tmp path.
    monkeypatch.setattr("config.HALT_PATH", str(tmp_path / "HALT"))

    # Price series ending well below entry → from_entry breaches core stop (-8%).
    idx = pd.date_range(end=dt.date.today(), periods=5, freq="B")
    df = pd.DataFrame({"AAPL": [100.0, 100.0, 100.0, 100.0, 80.0]}, index=idx)
    monkeypatch.setattr("data.fetch_prices", lambda tickers, period="6mo": df)

    portfolio = {"positions": [{
        "ticker": "AAPL", "shares": 3.5, "entry_price": 100.0,
        "entry_date": "", "tranche": "core",
    }], "cash": 0.0}

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 80.0)

    alerts = watchdog.check_price_moves(portfolio, broker=fb)

    sells = [o for o in fb._submitted if o.side == "sell"]
    assert len(sells) == 1
    assert sells[0].symbol == "AAPL"
    assert any("STOP ENFORCED" in a[2] for a in alerts)


def test_check_price_moves_alert_only_when_enforce_disabled(tmp_path, monkeypatch):
    import watchdog
    import pandas as pd
    from tests.fakes import FakeBroker

    monkeypatch.setattr("config.ENFORCE_STOPS", False)
    monkeypatch.setattr("config.HALT_PATH", str(tmp_path / "HALT"))

    idx = pd.date_range(end=dt.date.today(), periods=5, freq="B")
    df = pd.DataFrame({"AAPL": [100.0, 100.0, 100.0, 100.0, 80.0]}, index=idx)
    monkeypatch.setattr("data.fetch_prices", lambda tickers, period="6mo": df)

    portfolio = {"positions": [{
        "ticker": "AAPL", "shares": 3.5, "entry_price": 100.0,
        "entry_date": "", "tranche": "core",
    }], "cash": 0.0}

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 80.0)

    alerts = watchdog.check_price_moves(portfolio, broker=fb)

    assert [o for o in fb._submitted if o.side == "sell"] == []
    assert any("STOP-LOSS TRIGGERED" in a[2] for a in alerts)  # legacy alert intact
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_watchdog.py -k "enforces_stop or alert_only_when_enforce" -v`
Expected: FAIL — no sell is submitted; no `STOP ENFORCED` alert exists.

- [ ] **Step 4: Implement enforcement**

In `watchdog.py`, the existing stop/trailing block reads (lines 263-277):

```python
        # Stop-loss check
        if from_entry <= -stop_loss_pct:
            alerts.append((Alert.CRITICAL, t,
                f"STOP-LOSS TRIGGERED{tranche_label}: {from_entry:+.1f}% from entry ${entry:.2f} → ${current:.2f}. SELL NOW."))
        elif from_entry <= -stop_warn_pct:
            alerts.append((Alert.WARNING, t,
                f"Approaching stop-loss{tranche_label}: {from_entry:+.1f}% from entry"))

        # Trailing stop check
        if from_peak <= -trail_stop_pct:
            alerts.append((Alert.CRITICAL, t,
                f"TRAILING STOP HIT{tranche_label}: {from_peak:+.1f}% from peak ${peak:.2f}. Consider selling."))
        elif from_peak <= -trail_warn_pct:
            alerts.append((Alert.WARNING, t,
                f"Trailing stop warning{tranche_label}: {from_peak:+.1f}% from peak ${peak:.2f}"))
```

Immediately after that block (after the trailing-stop `elif`), insert the enforcement:

```python
        # ── Synthetic stop enforcement ──────────────────────────
        # Native stop/trailing orders can't attach to fractional shares, so we
        # enforce here with a market sell (which accepts fractional qty). Only
        # in the intraday run (broker supplied) and only when enabled + not
        # halted. Idempotent within a day via a deterministic client_order_id.
        breached = (from_entry <= -stop_loss_pct) or (from_peak <= -trail_stop_pct)
        if (breached and broker is not None and config.ENFORCE_STOPS
                and not os.path.exists(config.HALT_PATH)):
            cid = orders._make_cid(tranche, "stop-enforce", t, dt.date.today())
            try:
                broker.submit_market(t, qty=pos["shares"], side="sell",
                                     client_order_id=cid)
                orders._append_daily_log(
                    f"{dt.datetime.now(dt.timezone.utc).isoformat()},CLOSED,"
                    f"{t},{tranche},stop-enforced")
                alerts.append((Alert.CRITICAL, t,
                    f"STOP ENFORCED{tranche_label}: sold {pos['shares']} sh "
                    f"at ${current:.2f}."))
            except BrokerError as e:
                if "duplicate" not in str(e).lower():
                    alerts.append((Alert.CRITICAL, t,
                        f"STOP ENFORCE FAILED{tranche_label}: {e}"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_watchdog.py -k "enforces_stop or alert_only_when_enforce" -v`
Expected: PASS

- [ ] **Step 6: Run the existing price-moves test to confirm no regression**

Run: `pytest tests/test_watchdog.py -k check_price_moves -v`
Expected: PASS (including `test_check_price_moves_peak_excludes_pre_entry_history`, which passes `broker=None` → enforcement skipped).

- [ ] **Step 7: Commit**

```bash
git add watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): synthetic stop enforcement via market sell on breach"
```

---

### Task 5: Full suite + live dry-run verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: PASS (no regressions). If `test_rebalancer.py` has a test asserting `_system_equity` subtracts "unknown" MV, confirm it still constructs explicitly-unknown positions (adoption only affects `meta is None` at sync time, not already-tagged cache positions) — it should still pass. If it fails because it relied on sync producing "unknown", update it to seed an explicit `"tranche": "unknown"` cache entry.

- [ ] **Step 2: Dry-run sync_state against the real portfolio (read + classify, no orders)**

Run:

```bash
python3 -c "
import orders, config
from broker import Broker
alerts=[]
snap=orders.sync_state(Broker(env=config.ALPACA_ENV), alerts=alerts)
from collections import Counter
print('tranches:', Counter(p['tranche'] for p in snap.positions))
print('reasons :', Counter(p['entry_reason'] for p in snap.positions))
import rebalancer
print('system_equity (addressable):', round(rebalancer._system_equity(snap),2), 'of', round(snap.equity,2))
for a in alerts: print(' -', a)
"
```

Expected: all 14 positions show `tranche` in {core, aggressive} (SOXL, TECL → aggressive; rest → core), `entry_reason == "adopted"`, and `system_equity ≈ equity` (no longer ~$2,900). No "capital starved" alert.

- [ ] **Step 3: Commit (only if Step 1 required a test fix)**

```bash
git add -A && git commit -m "test: adjust rebalancer test for adoption-aware sync_state"
```

---

### Task 6: Update README

**Files:**
- Modify: `README.md`

**Interfaces:** documents `ADOPT_EXTERNAL_POSITIONS`, `UNKNOWN_MV_HALT_PCT`, `ENFORCE_STOPS` and the new intraday stop-enforcement behavior.

- [ ] **Step 1: Locate the config / behavior documentation section**

Run: `grep -nE "USE_LEVERAGED_ETFS|STOP_LOSS|trailing|watchdog|Config" README.md | head`

- [ ] **Step 2: Add documentation**

Under the configuration/flags section (or create a short "Position adoption & stop enforcement" subsection), add:

```markdown
### Position adoption & stop enforcement

- `ADOPT_EXTERNAL_POSITIONS` (default `True`): broker-imported positions with no
  local metadata are auto-tagged into a sleeve on `sync_state` — leveraged ETFs
  (`config.ETF_LEVERAGED`) → `aggressive`, everything else → `core`. This keeps
  the rebalancer's addressable capital aligned with the real book. Set `False`
  to keep such positions `unknown` (legacy behavior).
- `UNKNOWN_MV_HALT_PCT` (default `0.20`): if untagged positions exceed this
  share of equity, `sync_state` raises a CRITICAL "capital starved" alert.
- `ENFORCE_STOPS` (default `True`): the intraday watchdog submits a market sell
  on a stop-loss or trailing-stop breach. Market orders work on fractional
  shares, where native stop orders are rejected. The daily pre-market run
  remains alert-only.
```

- [ ] **Step 3: Verify and commit**

Run: `grep -n "ADOPT_EXTERNAL_POSITIONS" README.md`
Expected: the new line is present.

```bash
git add README.md
git commit -m "docs: document position adoption + stop-enforcement flags"
```

---

## Self-Review

**Spec coverage:**
- Part A (auto-adopt) → Task 2. ✓
- Part B (starvation guardrail) → Task 3. ✓
- Part C (synthetic enforced stop) → Task 4. ✓
- Part D (one-time reconciliation via next sync) → Task 5 Step 2 dry-run. ✓
- Config flags → Task 1. ✓
- README (standing user instruction) → Task 6. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; commands have expected output. ✓

**Type consistency:** `tranche` values `"core"/"aggressive"/"unknown"`; `entry_reason == "adopted"`; alert token `"capital starved"`; `broker.submit_market(symbol, qty=..., side="sell", client_order_id=...)` matches `FakeBroker` and real `Broker` signatures; `orders._make_cid(tranche, reason, symbol, today)` and `orders._append_daily_log(line)` match existing signatures. ✓
