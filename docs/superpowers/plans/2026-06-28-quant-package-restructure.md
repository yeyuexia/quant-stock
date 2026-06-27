# `quant/` Package Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended for this one — the move is a single atomic codemod, not parallelizable) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Move the 33 flat top-level modules into a `quant/` package with responsibility-based subpackages, with zero behavior change.

**Architecture:** A pure, mechanical reorg: `git mv` files into subpackages, re-anchor `.cache`/state paths to a new `quant/paths.py`, and run a deterministic codemod that rewrites import statements and test monkeypatch/patch string-targets (allowlist = our modules only). The **689-test suite is the behavior-preserving gate** — green before == green after.

**Tech Stack:** Python, pytest, a one-off codemod script (regex-based), git.

## Global Constraints

- **No function/logic edits.** The only body change permitted is re-anchoring `os.path.dirname(__file__)`-derived paths to `quant.paths.REPO_ROOT`. Everything else is import lines + test string-targets.
- Imports **alias to the original local name** (`import quant.data.market as data`) so module bodies are otherwise untouched.
- Codemod rewrites **only** names in the module map (verbatim below). Never rewrite third-party/stdlib (`yfinance`, `shutil`, `subprocess`, `alpaca`, `os`, `json`, `pandas`, `numpy`, …).
- String-target rewrite is scoped to the first string arg of `setattr(` / `patch(` calls (so filenames like `"orders.py"` in messages are not corrupted).
- `.cache`, `portfolio.json`, `daily_log.csv`, `watchlist_auto.json`, `.env` stay at repo root, untouched on disk.
- Success = the **same 689 tests pass** (6 integration deselected still collect), and a `python3 -m quant.<…>` entrypoint runs without ImportError.
- The module map (old → new dotted → import alias) is in the spec `docs/superpowers/specs/2026-06-28-quant-package-restructure-design.md` and reproduced in Task 3.
- Commit after each task.

---

### Task 1: Package skeleton + `paths.py`

**Files:**
- Create: `quant/__init__.py`, `quant/infra/__init__.py`, `quant/data/__init__.py`, `quant/signals/__init__.py`, `quant/strategies/__init__.py`, `quant/strategies/value/__init__.py`, `quant/agent/__init__.py`, `quant/execution/__init__.py`, `quant/risk/__init__.py`, `quant/monitor/__init__.py`, `quant/app/__init__.py`
- Create: `quant/paths.py`

- [ ] **Step 1: Create the package tree (empty `__init__.py` files)**

```bash
cd /Users/zl/works/stock
mkdir -p quant/infra quant/data quant/signals quant/strategies/value quant/agent quant/execution quant/risk quant/monitor quant/app
for d in quant quant/infra quant/data quant/signals quant/strategies quant/strategies/value quant/agent quant/execution quant/risk quant/monitor quant/app; do touch "$d/__init__.py"; done
```

- [ ] **Step 2: Create `quant/paths.py`**

```python
"""Single source of truth for on-disk locations. quant/ lives one level under
the repo root, so REPO_ROOT keeps .cache and state files exactly where they were
before the package move."""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, ".cache")
```

- [ ] **Step 3: Suite still green (nothing moved yet)**

Run: `python3 -m pytest -q 2>&1 | tail -2`
Expected: `689 passed, 6 deselected` (the empty package changes nothing).

- [ ] **Step 4: Commit**

```bash
git add quant/
git commit -m "refactor: add quant/ package skeleton + paths.py"
```

---

### Task 2: Write the codemod tool (dry-run only)

**Files:**
- Create: `/private/tmp/claude-501/-Users-zl-works-stock/5ebb7360-5fff-4a7b-aaa3-3799e4d4061b/scratchpad/codemod.py` (throwaway tool — NOT committed)

**Interfaces:**
- Produces: `codemod.py` exposing `MAP` (old→new dotted), `rewrite_text(text) -> text`, and a CLI that rewrites files in place when passed `--apply`.

- [ ] **Step 1: Write the codemod**

```python
#!/usr/bin/env python3
"""One-off codemod for the quant/ restructure. Rewrites import statements and
setattr/patch string-targets for OUR modules only (allowlist = MAP keys).
Usage: codemod.py [--apply] <files...>   (default = dry-run, prints changed files)."""
import re, sys

# old top-level module name -> new dotted path
MAP = {
  "config":"quant.config",
  "fileio":"quant.infra.fileio","notifications":"quant.infra.notifications","timeutils":"quant.infra.timeutils",
  "data":"quant.data.market","value_fundamentals":"quant.data.fundamentals","discovery":"quant.data.universe",
  "momentum":"quant.signals.momentum","screener":"quant.signals.screener","macro":"quant.signals.macro",
  "sentiment":"quant.signals.sentiment","indicators":"quant.signals.indicators","news_shock":"quant.signals.news_shock",
  "baseline":"quant.signals.baseline",
  "strategies":"quant.strategies.contract","value_screen":"quant.strategies.value.screen",
  "value_prefilter":"quant.strategies.value.prefilter","value_tracks":"quant.strategies.value.tracks",
  "investor_agent":"quant.agent.investor",
  "orders":"quant.execution.orders","broker":"quant.execution.broker","rebalancer":"quant.execution.rebalancer",
  "executor":"quant.execution.executor","planner":"quant.execution.planner","planning":"quant.execution.planning",
  "pending_plan":"quant.execution.pending_plan","breakers":"quant.execution.breakers",
  "sepa_exits":"quant.risk.sepa_exits","risk":"quant.risk.risk",
  "watchdog":"quant.monitor.watchdog",
  "run":"quant.app.daily_report","run_ensemble":"quant.app.ensemble","backtest":"quant.app.backtest",
}
NAMES = sorted(MAP, key=len, reverse=True)            # longest-first so value_screen beats value
ALT = "|".join(re.escape(n) for n in NAMES)

def rewrite_line_imports(line):
    # from OLD import ...   ->   from NEW import ...
    m = re.match(r'^(\s*from\s+)(' + ALT + r')(\s+import\b.*)$', line)
    if m: return f"{m.group(1)}{MAP[m.group(2)]}{m.group(3)}"
    # import OLD as X   ->   import NEW as X
    m = re.match(r'^(\s*import\s+)(' + ALT + r')(\s+as\s+\w+.*)$', line)
    if m: return f"{m.group(1)}{MAP[m.group(2)]}{m.group(3)}"
    # import OLD   ->   import NEW as OLD   (keep local name)
    m = re.match(r'^(\s*import\s+)(' + ALT + r')\s*$', line.rstrip("\n"))
    if m: return f"{m.group(1)}{MAP[m.group(2)]} as {m.group(2)}\n"
    return line

def rewrite_strings(text):
    # setattr("OLD.attr"... / patch("OLD.attr"...  ->  quant dotted. attr must be an identifier (skip "orders.py").
    def sub(m):
        return f"{m.group(1)}{MAP[m.group(2)]}.{m.group(3)}"
    pat = re.compile(r'((?:setattr|patch)\(\s*["\'])(' + ALT + r')\.([A-Za-z_]\w*)')
    return pat.sub(sub, text)

def rewrite_text(text):
    out = "".join(rewrite_line_imports(ln) for ln in text.splitlines(keepends=True))
    return rewrite_strings(out)

def main():
    apply = "--apply" in sys.argv
    files = [a for a in sys.argv[1:] if a != "--apply"]
    for f in files:
        src = open(f).read(); dst = rewrite_text(src)
        if dst != src:
            print(("APPLIED " if apply else "WOULD CHANGE ") + f)
            if apply: open(f, "w").write(dst)

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Unit-check the codemod on inline samples (no repo files touched)**

```bash
SC=/private/tmp/claude-501/-Users-zl-works-stock/5ebb7360-5fff-4a7b-aaa3-3799e4d4061b/scratchpad
python3 - "$SC/codemod.py" <<'PY'
import importlib.util, sys
spec=importlib.util.spec_from_file_location("cm", sys.argv[1]); cm=importlib.util.module_from_spec(spec); spec.loader.exec_module(cm)
assert cm.rewrite_text("import orders\n")=="import quant.execution.orders as orders\n"
assert cm.rewrite_text("from broker import BrokerError\n")=="from quant.execution.broker import BrokerError\n"
assert cm.rewrite_text("import data as d\n")=="import quant.data.market as d\n"
assert cm.rewrite_text('monkeypatch.setattr("orders.PORTFOLIO_PATH", x)\n')=='monkeypatch.setattr("quant.execution.orders.PORTFOLIO_PATH", x)\n'
assert cm.rewrite_text('msg = "orders.py failed"\n')=='msg = "orders.py failed"\n'      # filename untouched
assert cm.rewrite_text("import yfinance as yf\n")=="import yfinance as yf\n"             # third-party untouched
assert cm.rewrite_text("from value_screen import run\n")=="from quant.strategies.value.screen import run\n"
print("codemod self-check OK")
PY
```

Expected: `codemod self-check OK`

- [ ] **Step 3: (no commit — throwaway tool)**

---

### Task 3: Move files + re-anchor paths + run codemod (atomic; suite green)

**Files:** all 33 modules (git mv per the map), all `*.py` under `quant/`, `tests/`, `scripts/` (codemod), plus the `dirname(__file__)` path re-anchors.

- [ ] **Step 1: `git mv` every module to its new path**

```bash
cd /Users/zl/works/stock
git mv config.py quant/config.py
git mv fileio.py quant/infra/fileio.py
git mv notifications.py quant/infra/notifications.py
git mv timeutils.py quant/infra/timeutils.py
git mv data.py quant/data/market.py
git mv value_fundamentals.py quant/data/fundamentals.py
git mv discovery.py quant/data/universe.py
git mv momentum.py quant/signals/momentum.py
git mv screener.py quant/signals/screener.py
git mv macro.py quant/signals/macro.py
git mv sentiment.py quant/signals/sentiment.py
git mv indicators.py quant/signals/indicators.py
git mv news_shock.py quant/signals/news_shock.py
git mv baseline.py quant/signals/baseline.py
git mv strategies.py quant/strategies/contract.py
git mv value_screen.py quant/strategies/value/screen.py
git mv value_prefilter.py quant/strategies/value/prefilter.py
git mv value_tracks.py quant/strategies/value/tracks.py
git mv investor_agent.py quant/agent/investor.py
git mv orders.py quant/execution/orders.py
git mv broker.py quant/execution/broker.py
git mv rebalancer.py quant/execution/rebalancer.py
git mv executor.py quant/execution/executor.py
git mv planner.py quant/execution/planner.py
git mv planning.py quant/execution/planning.py
git mv pending_plan.py quant/execution/pending_plan.py
git mv breakers.py quant/execution/breakers.py
git mv sepa_exits.py quant/risk/sepa_exits.py
git mv risk.py quant/risk/risk.py
git mv watchdog.py quant/monitor/watchdog.py
git mv run.py quant/app/daily_report.py
git mv run_ensemble.py quant/app/ensemble.py
git mv backtest.py quant/app/backtest.py
```

- [ ] **Step 2: Run the codemod over the whole tree (imports + string-targets)**

```bash
SC=/private/tmp/claude-501/-Users-zl-works-stock/5ebb7360-5fff-4a7b-aaa3-3799e4d4061b/scratchpad
python3 "$SC/codemod.py" --apply $(git ls-files 'quant/*.py' 'tests/*.py' 'scripts/*.py') | tail -20
```

- [ ] **Step 3: Re-anchor `dirname(__file__)`-derived paths to `quant.paths`**

Find every site, then edit each so the base is `paths.REPO_ROOT` (add `from quant import paths` to those modules):

```bash
grep -rn "dirname(__file__)" quant/ | grep -vE "paths\.py"
```

For each hit that builds a repo-root/`.cache`/state path (e.g. `config.py`'s `HALT_PATH`/`PORTFOLIO_PATH`/`WATCHLIST_AUTO_PATH`, `quant/execution/orders.py`'s `PORTFOLIO_PATH`/`DAILY_LOG_PATH`, `quant/monitor/watchdog.py`'s sentinel/log paths, `quant/data/universe.py` cache dir, `quant/execution/pending_plan.py`, `quant/infra/notifications.py`, `quant/infra/fileio.py` if any), replace `os.path.dirname(__file__)` with `paths.REPO_ROOT` and add `from quant import paths` at the top. Example for `quant/config.py`:

```python
from quant import paths
...
HALT_PATH = os.path.join(paths.REPO_ROOT, ".cache", "HALT")     # was os.path.dirname(__file__)
```

(`os.path.dirname(__file__)` uses that build paths NOT rooted at the repo — e.g. relative to a data file beside the module — are rare here; leave those as-is. The grep output is the worklist; re-anchor only the repo-root/.cache/state ones.)

- [ ] **Step 4: Run the full suite — the behavior gate**

Run: `python3 -m pytest -q 2>&1 | tail -3`
Expected: `689 passed, 6 deselected`. If any ImportError or path test fails, fix the offending import/path (a missed lazy import inside a function, or a missed string-target), re-run until green.

- [ ] **Step 5: Sanity — no bare-module imports remain**

```bash
grep -rnE "^\s*(import|from) (orders|broker|config|watchdog|data|discovery|screener|strategies|value_screen|pending_plan|executor|rebalancer)\b" quant/ tests/ scripts/ | grep -v "quant\." | head
```
Expected: no output (every reference now goes through `quant.…`).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: move all modules into the quant/ package (codemod, no behavior change)"
```

---

### Task 4: Switch cron + `cron-wrapper.sh` to `python -m`, smoke-test

**Files:**
- Modify: `scripts/cron-wrapper.sh`
- Modify: the crontab (via `crontab -l`/`crontab -`)

- [ ] **Step 1: Make `cron-wrapper.sh` accept a `-m` module target**

`cron-wrapper.sh` currently ends with `exec python3 "$@"`. Confirm it still works when `"$@"` is `-m quant.monitor.watchdog [args]` (it does — `python3 -m quant.monitor.watchdog` is valid). No change needed unless it hard-codes a `.py` suffix; if it does, drop that. Verify:

```bash
sed -n '1,40p' scripts/cron-wrapper.sh
```

- [ ] **Step 2: Smoke-test each entrypoint under `-m` (import + arg parse, no live trading)**

```bash
cd /Users/zl/works/stock
for t in quant.monitor.watchdog quant.execution.rebalancer quant.execution.executor quant.data.universe quant.app.ensemble quant.app.daily_report; do
  python3 -c "import importlib; importlib.import_module('$t'); print('import OK: $t')"
done
python3 -m quant.execution.rebalancer --help >/dev/null 2>&1 && echo "rebalancer --help OK"
```
Expected: all `import OK` lines + the help line. (Avoid running watchdog/executor against the live broker here — import success is the gate.)

- [ ] **Step 3: Update the crontab targets**

```bash
crontab -l > /tmp/ct.bak
# replace each "cron-wrapper.sh <script>.py" with "cron-wrapper.sh -m quant.<area>.<module>"
sed -E \
  -e 's#cron-wrapper.sh watchdog.py#cron-wrapper.sh -m quant.monitor.watchdog#g' \
  -e 's#cron-wrapper.sh rebalancer.py#cron-wrapper.sh -m quant.execution.rebalancer#g' \
  -e 's#cron-wrapper.sh executor.py#cron-wrapper.sh -m quant.execution.executor#g' \
  -e 's#cron-wrapper.sh discovery.py#cron-wrapper.sh -m quant.data.universe#g' \
  -e 's#cron-wrapper.sh run_ensemble.py#cron-wrapper.sh -m quant.app.ensemble#g' \
  /tmp/ct.bak > /tmp/ct.new
crontab - < /tmp/ct.new
crontab -l | grep -E "quant\." | head
```
Expected: the trading cron lines now read `... cron-wrapper.sh -m quant.<area>.<module> ...`. (The `quant_review_local.py` job in `scripts/` is unchanged — it's a script path, not a moved module.)

- [ ] **Step 4: Commit**

```bash
git add scripts/cron-wrapper.sh
git commit -m "refactor: run cron entrypoints via python -m quant.* (+ crontab updated out-of-tree)"
```

---

### Task 5: Update docs to the new paths

**Files:**
- Modify: `README.md` (the Modules table + any `file.py` references), `docs/system_overview.html` (component `<code>` labels), `docs/architecture.html` (node `file:` labels), `docs/superpowers/plans/2026-06-27-discovery-...` references if any.

- [ ] **Step 1: Rewrite module references in docs**

Update the README "Modules" table and the two HTML docs so each module shows its new path (e.g. `orders.py` → `quant/execution/orders.py`, `watchdog.py` → `quant/monitor/watchdog.py`, `value_screen.py` → `quant/strategies/value/screen.py`). The architecture-graph node `file:` fields and system_overview `<code>` tags should show the new package paths.

- [ ] **Step 2: Verify the HTML still parses**

```bash
python3 -c "import html.parser; [html.parser.HTMLParser().feed(open('docs/'+f).read()) for f in ('system_overview.html','architecture.html')]; print('HTML OK')"
```
Expected: `HTML OK`

- [ ] **Step 3: Commit**

```bash
git add README.md docs/system_overview.html docs/architecture.html
git commit -m "docs: update module paths for the quant/ package layout"
```

---

## Self-Review

**Spec coverage:** package layout + paths.py (Task 1); module→path map + codemod for imports & string-targets (Tasks 2–3); path re-anchoring (Task 3 Step 3); suite-green gate (Task 3 Step 4); cron `python -m` switch + smoke-test (Task 4); docs (Task 5). ✓

**Placeholder scan:** every step has exact commands / code; the codemod is fully written; the git mv list is complete (33 modules). ✓

**Type consistency:** the `MAP` in the codemod matches the spec's module table verbatim; the git mv targets match `MAP` values; the cron `-m` targets match the new module paths (`quant.monitor.watchdog`, `quant.execution.rebalancer`, `quant.execution.executor`, `quant.data.universe`, `quant.app.ensemble`). ✓

**Note on execution:** Task 3 is a single atomic commit (the suite only goes green once everything moves together). Recommend **inline execution** (executing-plans) over parallel subagents — the codemod is one global operation gated by the suite, not independent tasks.
