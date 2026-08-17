"""Async wrapper so the Flask app can kick off a backtest and poll its status."""
import json
import logging
import os
import threading
import traceback
from typing import Dict, List, Optional

from ..utils.paths import data_path
from .data_fetcher import build_data_client
from .engine import run_backtest

logger = logging.getLogger(__name__)


def _results_path() -> str:
    return data_path("logs", "backtest_results.json")

_lock = threading.Lock()
_state = {
    "status": "idle",  # idle | running | done | error
    "progress_pct": 0.0,
    "message": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_worker: Optional[threading.Thread] = None


def get_state() -> Dict:
    with _lock:
        return dict(_state)


def _update(**kw):
    with _lock:
        _state.update(kw)


def load_last_results() -> Optional[Dict]:
    path = _results_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"Could not read backtest results: {exc}")
        return None


def _save_results(results: Dict):
    path = _results_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f)


def start_backtest(instruments: List[str], years: int = 5,
                   starting_balance: float = 10_000.0,
                   risk_pct: float = 1.0,
                   frictions: bool = True) -> Dict:
    """Kick off a backtest in a daemon thread if one isn't already running."""
    global _worker

    with _lock:
        if _state["status"] == "running":
            return {"ok": False, "error": "Backtest already running"}

    client = build_data_client()
    if client is None:
        return {
            "ok": False,
            "error": "OANDA credentials not configured — set OANDA_ACCOUNT_ID and OANDA_API_KEY in .env",
        }

    from datetime import datetime
    _update(status="running", progress_pct=0.0, message="Starting…",
            error=None, started_at=datetime.utcnow().isoformat(), finished_at=None)

    def _progress(msg: str, pct: float):
        _update(message=msg, progress_pct=round(pct, 1))

    def _run():
        try:
            results = run_backtest(
                client=client,
                instruments=instruments,
                years=years,
                starting_balance=starting_balance,
                risk_pct=risk_pct,
                frictions=frictions,
                progress_cb=_progress,
            )
            _save_results(results)
            _update(status="done", progress_pct=100.0, message="Complete",
                    finished_at=datetime.utcnow().isoformat())
        except Exception as exc:
            tb = traceback.format_exc()
            logger.exception("Backtest failed")
            _update(status="error", error=str(exc) + "\n" + tb,
                    finished_at=datetime.utcnow().isoformat())

    _worker = threading.Thread(target=_run, daemon=True)
    _worker.start()
    return {"ok": True}
