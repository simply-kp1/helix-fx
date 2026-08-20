import logging
from datetime import time
from typing import Optional, Dict, List
from ..utils.time_utils import to_london

logger = logging.getLogger(__name__)


def calculate_initial_balance(candles: List[Dict], ib_start_hour: int = 6) -> Optional[Dict]:
    """
    Find the two 30-min candles covering the first hour of the strategy session.

    Default ``ib_start_hour=6`` matches the FX London-open strategy (candles
    starting 06:00 and 06:30 London). Pass 8 for a DAX / Xetra-open variant.

    Returns {"high": float, "low": float} or None if the two candles aren't
    both present in the input.
    """
    ib_candles = []

    for candle in candles:
        if not candle.get("complete", True):
            continue

        candle_time_london = to_london(_parse_oanda_time(candle["time"]))
        t = candle_time_london.time()

        # Collect the two M30 candles that form the Initial Balance
        if t == time(ib_start_hour, 0) or t == time(ib_start_hour, 30):
            ib_candles.append(candle)

    if len(ib_candles) < 2:
        logger.debug(f"IB candles found: {len(ib_candles)} — need 2")
        return None

    highs = [float(c["mid"]["h"]) for c in ib_candles]
    lows = [float(c["mid"]["l"]) for c in ib_candles]

    return {
        "high": max(highs),
        "low": min(lows),
    }


def _parse_oanda_time(time_str: str):
    """Parse OANDA RFC3339 timestamp to datetime."""
    from datetime import datetime
    import pytz
    # OANDA format: "2024-01-15T06:00:00.000000000Z"
    clean = time_str[:19]  # "2024-01-15T06:00:00"
    dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
    return pytz.utc.localize(dt)
