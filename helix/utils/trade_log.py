import csv
import logging
import os
from datetime import datetime
from typing import Dict, List

import pytz

from .paths import data_path

logger = logging.getLogger(__name__)


def _csv_path() -> str:
    return data_path("trades.csv")

_COLUMNS = [
    "timestamp",
    "date",
    "instrument",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "units",
    "risk_price",
    "rr",
    "order_id",
    "account_balance",
    "daily_budget",
    "outcome",
    "pnl_usd",
    "close_time",
]


def _ensure_header():
    path = _csv_path()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writeheader()


def log_trade(order_id: str, instrument: str, direction: str, levels: Dict,
              units: int, account_balance: float, daily_budget: float):
    """Append one row to trades.csv whenever an order is placed."""
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)

    row = {
        "timestamp":       now.strftime("%Y-%m-%d %H:%M:%S"),
        "date":            now.strftime("%Y-%m-%d"),
        "instrument":      instrument,
        "direction":       direction,
        "entry":           f"{levels['entry']:.5f}",
        "stop_loss":       f"{levels['stop_loss']:.5f}",
        "take_profit":     f"{levels['target']:.5f}",
        "units":           abs(units),
        "risk_price":      f"{levels['risk']:.5f}",
        "rr":              levels["rr"],
        "order_id":        order_id,
        "account_balance": f"{account_balance:.2f}",
        "daily_budget":    f"{daily_budget:.2f}" if daily_budget else "",
        "outcome":         "PENDING",
        "pnl_usd":         "",
        "close_time":      "",
    }

    try:
        _ensure_header()
        path = _csv_path()
        with open(path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writerow(row)
        logger.info(f"Trade logged to CSV: {path}")
    except Exception as exc:
        logger.warning(f"Failed to write trade to CSV: {exc}")


def update_trade_outcomes(closed_trades: List[Dict]):
    """
    Match closed OANDA trades against PENDING rows in the CSV and fill in
    outcome / pnl_usd / close_time.

    closed_trades: list of dicts with keys:
        opening_order_id, realized_pl, close_time
    """
    path = _csv_path()
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        logger.warning(f"Could not read trades CSV for update: {exc}")
        return

    # Build lookup: opening_order_id → closed trade data
    closed_by_order: Dict[str, Dict] = {
        t["opening_order_id"]: t
        for t in closed_trades
        if t.get("opening_order_id")
    }

    updated = False
    for row in rows:
        if row.get("outcome") != "PENDING":
            continue
        match = closed_by_order.get(row.get("order_id", ""))
        if not match:
            continue

        pl = match["realized_pl"]
        row["outcome"]    = "WIN" if pl > 0 else "LOSS" if pl < 0 else "FLAT"
        row["pnl_usd"]    = f"{pl:.2f}"
        row["close_time"] = match.get("close_time", "")
        updated = True
        logger.info(
            f"Trade {row['order_id']} ({row['instrument']}) closed: "
            f"{row['outcome']} ${pl:.2f}"
        )

    if updated:
        try:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
        except Exception as exc:
            logger.warning(f"Failed to update trades CSV: {exc}")
