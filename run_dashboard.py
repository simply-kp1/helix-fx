"""
Helix Dashboard — starts the trading agent + web dashboard together.

Usage:
    python run_dashboard.py                  # paper mode, port 5000
    python run_dashboard.py --mode live      # live trading
    python run_dashboard.py --port 8080      # custom port
"""
import argparse
import logging
import os
import sys
import threading


def setup_logging():
    from helix.utils.paths import logs_dir
    log_file = os.path.join(logs_dir(), "helix.log")
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def build_agent(mode: str):
    """Construct the agent in the main thread so Flask can hold a reference."""
    from dotenv import load_dotenv
    load_dotenv(override=True)

    from helix.config import Config
    from helix.agent import HelixAgent
    from dashboard import state as shared_state

    config = Config.from_file("config.yaml")
    return HelixAgent(config, mode=mode, state_reporter=shared_state)


def main():
    # Load .env immediately so all os.environ reads below see the values
    from dotenv import load_dotenv
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Helix Dashboard")
    parser.add_argument("--mode",  choices=["live", "paper"],
                        default=os.environ.get("HELIX_MODE", "paper"))
    parser.add_argument("--port",  type=int,
                        default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--host",
                        default=os.environ.get("HOST", "0.0.0.0"))
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("helix.dashboard")

    if args.mode == "live":
        # Load env to check which OANDA environment we're targeting
        from dotenv import load_dotenv
        import os
        load_dotenv()
        oanda_env = os.getenv("OANDA_ENVIRONMENT", "practice").lower()

        if oanda_env == "live":
            # Real money — require explicit confirmation
            confirm = input(
                "\n WARNING: OANDA_ENVIRONMENT=live — real money orders will be placed!\n"
                "   Type YES to confirm: "
            )
            if confirm.strip() != "YES":
                log.info("Live mode cancelled.")
                sys.exit(0)
        else:
            log.info("Connecting to OANDA practice account (demo — no real money)")

    # Build agent in the main thread so Flask can call approve/reject on it.
    agent = build_agent(args.mode)

    # Run the agent's scheduling loop in a background daemon thread.
    agent_thread = threading.Thread(target=agent.run, daemon=True)
    agent_thread.start()
    log.info(f"Helix agent started in background ({args.mode} mode)")

    # Start Flask in the main thread
    from dashboard.app import create_app
    app = create_app(agent=agent)

    log.info(f"Dashboard running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
