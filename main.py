"""
Helix FX Trading Agent — Entry Point

Usage:
    python main.py                    # paper mode, all configured instruments
    python main.py --mode live        # live trading (real orders)
    python main.py --instrument EUR_USD
    python main.py --config my_config.yaml
"""
import argparse
import logging
import sys
from pathlib import Path


def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler("logs/helix.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Helix FX Trading Agent")
    
    parser.add_argument(
    "--once",
    action="store_true",
    help="Run one Helix tick and exit",
)
    
    parser.add_argument(
        "--mode",
        choices=["live", "practice", "paper"],
        default="practice",
        help="Trading mode: paper, practice, or live (default: paper)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML (default: config.yaml)",
    )
    parser.add_argument(
        "--instrument",
        default=None,
        help="Override instruments with a single pair, e.g. EUR_USD",
    )
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("helix.main")

    if args.mode == "live":
        confirm = input(
            "\n⚠  LIVE mode: real orders will be placed on your OANDA account.\n"
            "   Type 'YES' to confirm: "
        )
        if confirm.strip() != "YES":
            logger.info("Live mode cancelled by user.")
            sys.exit(0)

    from helix.config import Config
    from helix.agent import HelixAgent

    config = Config.from_file(args.config)
    agent = HelixAgent(config, mode=args.mode)

    if args.instrument:
        agent.instruments = [args.instrument]
    if args.once:
        agent.tick()
    else:
        agent.run()


if __name__ == "__main__":
    main()
