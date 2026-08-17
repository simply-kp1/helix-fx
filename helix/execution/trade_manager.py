import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class TradeManager:
    """Tracks per-day trade counts and manages order lifecycles."""

    def __init__(self, client):
        self.client = client

    def get_open_orders(self, instrument: str) -> List[Dict]:
        try:
            return self.client.get_open_orders(instrument)
        except Exception as e:
            logger.error(f"[{instrument}] Could not fetch open orders: {e}")
            return []

    def cancel_all_pending(self, instrument: str, order_manager) -> int:
        """Cancel all pending stop orders for an instrument. Returns count cancelled."""
        orders = self.get_open_orders(instrument)
        cancelled = 0
        for order in orders:
            if order_manager.cancel_order(instrument, order["id"]):
                cancelled += 1
        if cancelled:
            logger.info(f"[{instrument}] Cancelled {cancelled} pending orders at session end")
        return cancelled
