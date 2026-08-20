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

    Sizing so that a stop-loss hit ≈ ``risk_percent`` % of ``account_balance``:

      * FX XXX/USD pairs (EUR/USD, GBP/USD, AUD/USD, NZD/USD):
            units = risk_amount / stop_distance
      * FX USD/XXX pairs (USD/JPY, USD/CHF, USD/CAD):
            same formula; JPY-quoted sizing is slightly off because pip-value
            depends on price, but the pre-existing agent has always used
            this simplified form — kept for parity.
      * CFD indices quoted in a non-USD currency (DE30_EUR, EU50_EUR,
        UK100_GBP, JP225_JPY, ...):
            1 unit ≈ 1 quote-currency per point of movement, so
            units = risk_amount / stop_distance still works IF
            ``account_balance`` is expressed in the SAME currency as the
            quote (or you accept the small FX-conversion drift). For a
            USD account trading DE30_EUR, actual USD risk will differ
            from risk_percent by the EUR/USD rate.
    """
    risk_amount = account_balance * (risk_percent / 100.0)
    stop_distance = abs(entry - stop_loss)

    if stop_distance == 0:
        return 0

    units = int(risk_amount / stop_distance)
    units = max(units, 1)

    return units if direction == "long" else -units
