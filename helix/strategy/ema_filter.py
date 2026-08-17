import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


def calculate_ema(candles: List[Dict], period: int = 100) -> float:
    """Calculate EMA from a list of complete OANDA candles (most-recent last)."""
    closes = [float(c["mid"]["c"]) for c in candles if c.get("complete", True)]

    if len(closes) < period:
        logger.warning(f"Only {len(closes)} candles for EMA-{period}; using SMA fallback")
        return sum(closes) / len(closes) if closes else 0.0

    # Seed with SMA of first `period` values
    ema = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)

    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def check_ema_filter(ema_value: float, entry: float, target: float, direction: str) -> bool:
    """
    Returns True  → trade is allowed (EMA does not block the path to target).
    Returns False → skip trade (EMA sits between entry and target, acting as a barrier).

    Long:  if entry < EMA < target  → EMA acts as resistance → skip
    Short: if target < EMA < entry  → EMA acts as support    → skip
    """
    if direction == "long":
        if entry < ema_value < target:
            logger.info(
                f"EMA filter: LONG blocked — EMA {ema_value:.5f} is between "
                f"entry {entry:.5f} and target {target:.5f}"
            )
            return False

    elif direction == "short":
        if target < ema_value < entry:
            logger.info(
                f"EMA filter: SHORT blocked — EMA {ema_value:.5f} is between "
                f"target {target:.5f} and entry {entry:.5f}"
            )
            return False

    return True
