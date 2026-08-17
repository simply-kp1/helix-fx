import logging
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


def calculate_atr(candles: List[Dict], period: int = 14) -> float:
    """Calculate Average True Range from daily OANDA candles."""
    if len(candles) < period + 1:
        logger.warning(f"Not enough candles for ATR-{period}")
        return 0.0

    true_ranges = []
    prev_close = float(candles[0]["mid"]["c"])

    for candle in candles[1:]:
        high = float(candle["mid"]["h"])
        low = float(candle["mid"]["l"])
        close = float(candle["mid"]["c"])

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)
        prev_close = close

    # Smooth with Wilder's method (same as ta-lib)
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


def calculate_trade_levels(
    direction: str,
    ib_high: float,
    ib_low: float,
    daily_atr: float,
    atr_buffer_pct: float = 0.05,
    min_rr: float = 1.5,
) -> Optional[Dict]:
    """
    Returns trade levels dict or None if minimum R:R cannot be achieved.

    Long:  entry=IB High, SL=IB Low - buffer, TP=entry + risk*min_rr
    Short: entry=IB Low,  SL=IB High + buffer, TP=entry - risk*min_rr
    """
    buffer = daily_atr * atr_buffer_pct

    if direction == "long":
        entry = ib_high
        stop_loss = ib_low - buffer
        risk = entry - stop_loss

        if risk <= 0:
            logger.warning("Long risk <= 0; skipping trade")
            return None

        target = entry + (risk * min_rr)

        return {
            "direction": "long",
            "entry": entry,
            "stop_loss": stop_loss,
            "target": target,
            "risk": risk,
            "rr": min_rr,
        }

    elif direction == "short":
        entry = ib_low
        stop_loss = ib_high + buffer
        risk = stop_loss - entry

        if risk <= 0:
            logger.warning("Short risk <= 0; skipping trade")
            return None

        target = entry - (risk * min_rr)

        return {
            "direction": "short",
            "entry": entry,
            "stop_loss": stop_loss,
            "target": target,
            "risk": risk,
            "rr": min_rr,
        }

    return None


def calculate_position_size(
    entry: float,
    stop_loss: float,
    account_balance: float,
    risk_percent: float,
    direction: str,
    instrument: str = "EUR_USD",
) -> int:
    """
    Returns OANDA units (positive=long, negative=short).

    Simplified pip-value calculation:
      - For XXX/USD pairs: pip_value = 0.0001 per unit
      - For USD/JPY:       pip_value = 0.01 / current_price per unit
    Assumes USD-denominated account.
    """
    risk_amount = account_balance * (risk_percent / 100.0)
    stop_distance = abs(entry - stop_loss)

    if stop_distance == 0:
        return 0

    # Units = risk_amount / stop_distance  (works directly for USD-quoted pairs)
    units = int(risk_amount / stop_distance)
    units = max(units, 1)

    return units if direction == "long" else -units
