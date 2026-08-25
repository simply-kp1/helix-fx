"""CLI entry point for running a Helix backtest from GitHub Actions."""
import argparse
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--starting-balance", type=float, default=10_000.0)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--frictions", type=lambda v: v.lower() != "false", default=True)
    args = parser.parse_args()

    from helix.backtest.data_fetcher import build_data_client
    from helix.backtest.engine import run_backtest

    client = build_data_client()
    if client is None:
        logger.error("OANDA credentials not set — export OANDA_ACCOUNT_ID and OANDA_API_KEY")
        sys.exit(1)

    def progress(msg: str, pct: float):
        logger.info(f"[{pct:5.1f}%] {msg}")

    logger.info(
        f"Starting backtest: years={args.years}, balance={args.starting_balance}, "
        f"risk={args.risk_pct}%, frictions={args.frictions}"
    )

    results = run_backtest(
        client=client,
        instruments=["DE30_EUR"],
        years=args.years,
        starting_balance=args.starting_balance,
        risk_pct=args.risk_pct,
        frictions=args.frictions,
        progress_cb=progress,
    )

    out_path = os.path.join("state", "backtest_results.json")
    os.makedirs("state", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Results saved to {out_path}")
    overall = results.get("overall", {})
    logger.info(
        f"Trades: {overall.get('total_trades', 0)}  "
        f"Win rate: {overall.get('win_rate_pct', 0):.1f}%  "
        f"Net P&L: ${overall.get('net_pnl_usd', 0):,.2f}"
    )


if __name__ == "__main__":
    main()
