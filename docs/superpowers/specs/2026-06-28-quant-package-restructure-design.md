# Design: `quant/` package restructure (pure reorg)

**Date:** 2026-06-28
**Status:** Approved (design)
**Scope:** Move the ~33 flat top-level modules into a `quant/` package with
responsibility-based subpackages, rewrite all imports + test string-targets,
re-anchor `.cache`/state paths, and switch the cron/entrypoints to `python -m`.
**No function or logic changes** — the 689-test suite is the behavior-preserving
proof: green before == green after.

## Background

The repo is ~33 flat top-level scripts (~10.5k LOC) with a flat import namespace
(`import orders`, `from broker import …`). There are no package boundaries; the
giant files (`watchdog` 1529, `orders` 1154, `discovery` 1054) and small ones sit
side by side. The user wants a clear, well-organized package layout.

## Goals

- A `quant/` package whose folders read as the system's flow:
  **data → signals → strategies → agent → execution → risk → monitor**, plus
  `infra/` (plumbing) and `app/` (runnable entrypoints).
- Behavior identical — verified by the unchanged test suite at each milestone.
- `.cache`/state files stay exactly where they are today (repo root).
- The live cron switches to `python -m quant.…` targets, smoke-tested.

## Non-goals

- No splitting of the god-files (watchdog/orders/discovery) — separate later effort.
- No logic, behavior, or API changes; no new features.
- `scripts/` stays where it is (its imports are rewritten, files not moved).

## Target layout

```
quant/
  __init__.py
  config.py
  paths.py                 NEW — REPO_ROOT / CACHE_DIR anchor
  infra/   fileio.py · notifications.py · timeutils.py
  data/    market.py(=data) · fundamentals.py(=value_fundamentals) · universe.py(=discovery)
  signals/ momentum · screener · macro · sentiment · indicators · news_shock · baseline
  strategies/ contract.py(=strategies) · value/{screen,prefilter,tracks}.py
  agent/   investor.py(=investor_agent)
  execution/ orders · broker · rebalancer · executor · planner · planning · pending_plan · breakers
  risk/    sepa_exits.py · risk.py
  monitor/ watchdog.py
  app/     daily_report.py(=run) · ensemble.py(=run_ensemble) · backtest.py
```

### Full module → new path map (the codemod's source of truth)

| old (top-level) | new dotted path | import alias |
|---|---|---|
| config | quant.config | config |
| fileio | quant.infra.fileio | fileio |
| notifications | quant.infra.notifications | notifications |
| timeutils | quant.infra.timeutils | timeutils |
| data | quant.data.market | data |
| value_fundamentals | quant.data.fundamentals | value_fundamentals |
| discovery | quant.data.universe | discovery |
| momentum | quant.signals.momentum | momentum |
| screener | quant.signals.screener | screener |
| macro | quant.signals.macro | macro |
| sentiment | quant.signals.sentiment | sentiment |
| indicators | quant.signals.indicators | indicators |
| news_shock | quant.signals.news_shock | news_shock |
| baseline | quant.signals.baseline | baseline |
| strategies | quant.strategies.contract | strategies |
| value_screen | quant.strategies.value.screen | value_screen |
| value_prefilter | quant.strategies.value.prefilter | value_prefilter |
| value_tracks | quant.strategies.value.tracks | value_tracks |
| investor_agent | quant.agent.investor | investor_agent |
| orders | quant.execution.orders | orders |
| broker | quant.execution.broker | broker |
| rebalancer | quant.execution.rebalancer | rebalancer |
| executor | quant.execution.executor | executor |
| planner | quant.execution.planner | planner |
| planning | quant.execution.planning | planning |
| pending_plan | quant.execution.pending_plan | pending_plan |
| breakers | quant.execution.breakers | breakers |
| sepa_exits | quant.risk.sepa_exits | sepa_exits |
| risk | quant.risk.risk | risk |
| watchdog | quant.monitor.watchdog | watchdog |
| run | quant.app.daily_report | run |
| run_ensemble | quant.app.ensemble | run_ensemble |
| backtest | quant.app.backtest | backtest |

Files are renamed for clarity, but **import statements alias to the original
local name** (`import quant.data.market as data`) so module *bodies* are
untouched — only import lines and test string-targets change. This keeps the
move mechanical and the diff reviewable.

## The three risky pieces

### 1. Path re-anchoring (the landmine)
Many modules build paths from `os.path.dirname(__file__)` (e.g. `config.PORTFOLIO_PATH`,
`HALT_PATH`, `.cache/*`, `watchlist_auto.json`, `daily_log.csv`). Moving files
one level deeper would relocate all of it. **Fix:** add `quant/paths.py`:
```python
import os
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # quant/ → repo root
CACHE_DIR = os.path.join(REPO_ROOT, ".cache")
```
Then re-point every `os.path.dirname(__file__)`-derived path to `paths.REPO_ROOT`
(or `paths.CACHE_DIR`). A grep enumerates the sites (config, orders, watchdog,
discovery, pending_plan, notifications, fileio, …). State files stay at repo root.

### 2. Global import + string-target rewrite (deterministic codemod)
A `python` codemod driven by the map above, applied to every `.py` under `quant/`,
`tests/`, and `scripts/`:
- **Import statements** (top-level AND inside functions — several modules import
  lazily): `import X` → `import <dotted> as X`; `import X as Y` → `import <dotted> as Y`;
  `from X import …` → `from <dotted> import …`.
- **Test string-targets:** `monkeypatch.setattr("X.ATTR", …)` and
  `patch("X.ATTR")` → `"<dotted>.ATTR"`. (~400 sites; 99×`orders`, 60×`config`,
  54×`pending_plan`, …)
- **Allowlist only:** rewrite ONLY names in the map. Third-party / stdlib names
  (`yfinance`, `shutil`, `subprocess`, `alpaca`, `os`, `json`, …) are never touched.

### 3. Entrypoints / cron (`python -m`)
Each entrypoint module keeps its `if __name__ == "__main__": main()` (works under
`-m`). Update **crontab + `scripts/cron-wrapper.sh`**:
| cron job | new target |
|---|---|
| watchdog.py | `python3 -m quant.monitor.watchdog` |
| watchdog.py --intraday | `python3 -m quant.monitor.watchdog --intraday` |
| rebalancer.py --tranche … | `python3 -m quant.execution.rebalancer --tranche …` |
| executor.py | `python3 -m quant.execution.executor` |
| discovery.py --update | `python3 -m quant.data.universe --update` |
| run_ensemble.py | `python3 -m quant.app.ensemble` |
`cron-wrapper.sh` already `cd`s to repo root + sources `.env`; it just needs to run
`python3 -m "$@"`-style args instead of a script path. Each `-m` target is
**smoke-tested** manually (`--quick`/`--dry-run`) before the cron relies on it.

## Migration order (each milestone ends with the suite green)

1. `quant/` skeleton (`__init__.py` per subpackage) + `paths.py`.
2. `git mv` every module to its new path (history preserved).
3. Re-anchor `dirname(__file__)` paths to `paths.REPO_ROOT`.
4. Run the codemod (imports + test string-targets).
5. `python3 -m pytest` → **green** (proves behavior unchanged).
6. Switch crontab + `cron-wrapper.sh` to `-m`; smoke-test each entrypoint.
7. Update docs (README module table, `docs/system_overview.html`,
   `docs/architecture.html` file labels) to the new paths.

## Error handling / safety

The test suite is the gate: any import or path mistake surfaces as a red suite,
not a silent production break. The cron switch is the one ops-sensitive step and
is smoke-tested before reliance. `.env`, `.cache`, and all state files are
untouched on disk.

## Testing

No new tests. Success = the **same 689 tests pass** after the move (and the 6
deselected integration tests still collect). A representative `-m` entrypoint
runs without import error (`python3 -m quant.monitor.watchdog --quick` against the
paper account, or `--help`).

## Build phases (for the implementation plan)

1. Package skeleton + `paths.py` + path re-anchoring.
2. `git mv` all modules + the import/string-target codemod; suite green.
3. Cron/`cron-wrapper.sh` switch + entrypoint smoke-tests.
4. Docs path updates.
