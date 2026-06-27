#!/usr/bin/env python3
"""Daily ensemble pipeline: run each strategy in isolation, then have the agent
pick the top-N buy candidates. Run before market open; the intraday watchdog
reads .cache/buy_candidates.json and decides entries.
"""
import logging
import strategies
import investor_agent

logging.basicConfig(level=logging.INFO)


def main():
    paths = strategies.run_strategies(strategies.default_registry())
    print(f"strategies written: {paths}")
    picks = investor_agent.select_candidates()
    for p in picks:
        print(f"  {p['ticker']:<6} [{','.join(p['strategies'])}] {p['rationale']}")


if __name__ == "__main__":
    main()
