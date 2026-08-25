import logging
from typing import Optional

logger = logging.getLogger(__name__)

PRICE_DECIMALS = {
    "EUR_USD": 5,
    "GBP_USD": 5,
    "USD_JPY": 3,
    "AUD_USD": 5,
    "USD_CAD": 5,
    "USD_CHF": 5,
    "NZD_USD": 5,
    "DE30_EUR": 1,  # DAX index — OANDA expects 1 decimal place
}


def _fmt(instrument: str, price: float) -> str:
    decimals = PRICE_DECIMALS.get(instrument, 5)
    return f"{price:.{decimals}f}"


class OrderManager:
    def __init__(self, client, mode: str = "paper"):
        self.client = client
        self.mode = mode

    def place_stop_order(
        self,
        instrument: str,
        direction: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        units: int,
        gtd_time: str,
    ) -> Optional[str]:
        try:
            order_id = self.client.create_stop_order(
                instrument=instrument,
                units=units,
                price=_fmt(instrument, entry),
                stop_loss=_fmt(instrument, stop_loss),
                take_profit=_fmt(instrument, take_profit),
                gtd_time=gtd_time,
            )
            dec = PRICE_DECIMALS.get(instrument, 5)
            logger.info(
                f"[{instrument}] {direction.upper()} stop order placed "
                f"id={order_id} entry={entry:.{dec}f} sl={stop_loss:.{dec}f} tp={take_profit:.{dec}f}"
            )
            return order_id
        except Exception as e:
            logger.error(f"[{instrument}] Failed to place order: {e}")
            return None

    def cancel_order(self, instrument: str, order_id: str) -> bool:
        try:
            result = self.client.cancel_order(order_id)
            if result:
                logger.info(f"[{instrument}] Order {order_id} cancelled")
            return result
        except Exception as e:
            logger.error(f"[{instrument}] Failed to cancel order {order_id}: {e}")
            return False
