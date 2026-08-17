import logging
from typing import Optional, Dict
from ..utils.time_utils import to_london

logger = logging.getLogger(__name__)


def detect_breakout(candle: Dict, ib_high: float, ib_low: float) -> Optional[str]:
    """
    Returns 'long' if candle closed above IB High,
    'short' if closed below IB Low, else None.
    Only considers complete candles after 07:00 UK time.
    """
    if not candle.get("complete", True):
        return None

    close = float(candle["mid"]["c"])

    if close > ib_high:
        logger.info(f"Breakout LONG detected: close={close:.5f} > IB High={ib_high:.5f}")
        return "long"

    if close < ib_low:
        logger.info(f"Breakout SHORT detected: close={close:.5f} < IB Low={ib_low:.5f}")
        return "short"

    return None
