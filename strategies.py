"""Isolated-strategy contract: each strategy writes its own result file under
.cache/strategies/, and the agent reads them all. One strategy failing never
affects the others.
"""
import datetime as dt
import json
import logging
import os

_log = logging.getLogger(__name__)

STRATEGIES_DIR = os.path.join(os.path.dirname(__file__), ".cache", "strategies")


def write_strategy_result(name: str, rows: list) -> str:
    """Persist one strategy's rows to .cache/strategies/<name>.json."""
    os.makedirs(STRATEGIES_DIR, exist_ok=True)
    path = os.path.join(STRATEGIES_DIR, f"{name}.json")
    payload = {
        "strategy": name,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rows": rows,
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return path


def load_strategy_results() -> dict:
    """Return {name: parsed_json} for every readable file. Fail-open per file."""
    out: dict = {}
    if not os.path.isdir(STRATEGIES_DIR):
        return out
    for fname in os.listdir(STRATEGIES_DIR):
        if not fname.endswith(".json"):
            continue
        name = fname[:-len(".json")]
        try:
            with open(os.path.join(STRATEGIES_DIR, fname)) as f:
                out[name] = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("load_strategy_results: skipping %s: %s", fname, e)
    return out


def run_strategies(registry: dict) -> list:
    """Run each registered name->callable() that returns rows; write each.
    A callable that raises is logged and skipped (isolation)."""
    paths = []
    for name, fn in registry.items():
        try:
            rows = fn()
        except Exception as e:
            _log.warning("run_strategies: strategy %s failed: %s", name, e)
            continue
        paths.append(write_strategy_result(name, rows or []))
    return paths
