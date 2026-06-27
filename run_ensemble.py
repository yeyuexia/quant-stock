#!/usr/bin/env python3
"""Daily ensemble pipeline: run each strategy in isolation, then have the agent
pick the top-N buy candidates. Run before market open; the intraday watchdog
reads .cache/buy_candidates.json and decides entries.
"""
import logging
import strategies
import investor_agent

logging.basicConfig(level=logging.INFO)


def run():
    """Run every registered strategy (isolated, in parallel) then the agent
    selection. Returns the agent's picks. Used by both the CLI and the daily
    watchdog so candidate generation has a single code path.
    """
    strategies.run_strategies(strategies.default_registry())
    return investor_agent.select_candidates()


def main():
    picks = run()
    for p in picks:
        print(f"  {p['ticker']:<6} [{','.join(p['strategies'])}] {p['rationale']}")


if __name__ == "__main__":
    main()
