import logging
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class OandaClient:
    """Live OANDA v20 REST API client."""

    def __init__(self, account_id: str, api_key: str, environment: str = "practice"):
        import oandapyV20
        self.account_id = account_id
        self.api = oandapyV20.API(access_token=api_key, environment=environment)

    def get_candles(
        self,
        instrument: str,
        granularity: str = "M30",
        count: int = 200,
    ) -> List[Dict]:
        from oandapyV20.endpoints.instruments import InstrumentsCandles

        params = {"count": count, "granularity": granularity, "price": "M"}
        req = InstrumentsCandles(instrument=instrument, params=params)
        self.api.request(req)
        return req.response.get("candles", [])

    def get_account_balance(self) -> float:
        from oandapyV20.endpoints.accounts import AccountDetails

        req = AccountDetails(accountID=self.account_id)
        self.api.request(req)
        return float(req.response["account"]["balance"])

    def create_stop_order(
        self,
        instrument: str,
        units: int,
        price: str,
        stop_loss: str,
        take_profit: str,
        gtd_time: str,
    ) -> Optional[str]:
        from oandapyV20.endpoints.orders import OrderCreate

        data = {
            "order": {
                "type": "STOP",
                "instrument": instrument,
                "units": str(units),
                "price": price,
                "stopLossOnFill": {"price": stop_loss},
                "takeProfitOnFill": {"price": take_profit},
                "timeInForce": "GTD",
                "gtdTime": gtd_time,
            }
        }
        req = OrderCreate(accountID=self.account_id, data=data)
        self.api.request(req)
        resp = req.response
        order_id = resp.get("orderCreateTransaction", {}).get("id")
        logger.info(f"Order created: id={order_id}")
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        from oandapyV20.endpoints.orders import OrderCancel

        req = OrderCancel(accountID=self.account_id, orderID=order_id)
        try:
            self.api.request(req)
            return True
        except Exception as e:
            logger.error(f"Cancel order {order_id} failed: {e}")
            return False

    def get_open_orders(self, instrument: str) -> List[Dict]:
        from oandapyV20.endpoints.orders import OrdersPending

        req = OrdersPending(accountID=self.account_id)
        self.api.request(req)
        orders = req.response.get("orders", [])
        return [o for o in orders if o.get("instrument") == instrument]

    def get_closed_trades(self, count: int = 500, instrument: str = "DE30_EUR") -> List[Dict]:
        """Return closed trades with opening_order_id and realized_pl for CSV matching."""
        from oandapyV20.endpoints.trades import TradesList

        params = {"state": "CLOSED", "count": str(count), "instrument": instrument}
        req = TradesList(accountID=self.account_id, params=params)
        self.api.request(req)
        result = []
        for t in req.response.get("trades", []):
            pl = float(t.get("realizedPL", 0))
            result.append({
                "opening_order_id": t.get("openingOrderID", ""),
                "realized_pl":      pl,
                "close_time":       t.get("closeTime", ""),
            })
        return result

    def get_pnl_by_day(self, days: int = 3, instrument: str = "DE30_EUR") -> List[Dict]:
        """Return daily P&L summary for the last `days` calendar days (London time)."""
        import pytz
        from collections import defaultdict
        from oandapyV20.endpoints.trades import TradesList

        london = pytz.timezone("Europe/London")
        today = datetime.now(london).date()

        params = {"state": "CLOSED", "count": "500", "instrument": instrument}
        req = TradesList(accountID=self.account_id, params=params)
        self.api.request(req)
        trades = req.response.get("trades", [])

        daily: Dict = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
        for trade in trades:
            close_str = trade.get("closeTime") or trade.get("openTime", "")
            if not close_str:
                continue
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                close_date = close_dt.astimezone(london).date()
            except Exception:
                continue
            pl = float(trade.get("realizedPL", 0))
            daily[close_date]["pnl"] += pl
            daily[close_date]["trades"] += 1
            if pl > 0:
                daily[close_date]["wins"] += 1
            elif pl < 0:
                daily[close_date]["losses"] += 1

        result = []
        for i in range(days):
            d = today - timedelta(days=i)
            label = "TODAY" if i == 0 else "YESTERDAY" if i == 1 else d.strftime("%a %d %b").upper()
            day = daily.get(d, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
            result.append({
                "date": d.isoformat(),
                "label": label,
                "pnl": round(day["pnl"], 2),
                "trades": day["trades"],
                "wins": day["wins"],
                "losses": day["losses"],
            })
        return result


class MockOandaClient:
    """
    Paper-trading client — logs all actions, returns realistic synthetic data.
    No real orders are placed.
    """

    def __init__(self):
        self._order_counter = 1000
        self._open_orders: Dict[str, Dict] = {}
        self._balance = 10_000.0

    def get_candles(
        self,
        instrument: str,
        granularity: str = "M30",
        count: int = 200,
    ) -> List[Dict]:
        """Generate synthetic 30-min candles anchored to realistic EUR/USD prices."""
        import pytz

        base_prices = {
            "EUR_USD": 1.0850,
            "GBP_USD": 1.2700,
            "USD_JPY": 149.50,
        }
        base = base_prices.get(instrument, 1.0000)

        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
        candles = []

        minutes_step = {"M30": 30, "D": 1440}.get(granularity, 30)

        for i in range(count, 0, -1):
            candle_time = now_utc - timedelta(minutes=i * minutes_step)

            drift = random.gauss(0, 0.0005)
            base = round(base + drift, 5)

            spread = random.uniform(0.0003, 0.0010)
            high = round(base + spread, 5)
            low = round(base - spread, 5)
            open_ = round(random.uniform(low, high), 5)
            close = round(random.uniform(low, high), 5)

            is_complete = i > 1  # last candle is in-progress

            candles.append(
                {
                    "time": candle_time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                    "mid": {
                        "o": f"{open_:.5f}",
                        "h": f"{high:.5f}",
                        "l": f"{low:.5f}",
                        "c": f"{close:.5f}",
                    },
                    "volume": random.randint(500, 5000),
                    "complete": is_complete,
                }
            )

        return candles

    def get_account_balance(self) -> float:
        return self._balance

    def create_stop_order(
        self,
        instrument: str,
        units: int,
        price: str,
        stop_loss: str,
        take_profit: str,
        gtd_time: str,
    ) -> str:
        order_id = str(self._order_counter)
        self._order_counter += 1
        self._open_orders[order_id] = {
            "id": order_id,
            "instrument": instrument,
            "units": units,
            "price": price,
            "stopLoss": stop_loss,
            "takeProfit": take_profit,
            "gtdTime": gtd_time,
            "state": "PENDING",
        }
        direction = "LONG" if units > 0 else "SHORT"
        logger.info(
            f"[PAPER] {direction} STOP order #{order_id} | {instrument} | "
            f"Entry={price} SL={stop_loss} TP={take_profit} Units={abs(units)}"
        )
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._open_orders:
            inst = self._open_orders[order_id]["instrument"]
            del self._open_orders[order_id]
            logger.info(f"[PAPER] Order #{order_id} ({inst}) cancelled")
            return True
        return False

    def get_open_orders(self, instrument: str) -> List[Dict]:
        return [
            o
            for o in self._open_orders.values()
            if o["instrument"] == instrument and o["state"] == "PENDING"
        ]

    def get_closed_trades(self, count: int = 500) -> List[Dict]:
        return []  # paper trades don't close through OANDA

    def get_pnl_by_day(self, days: int = 3) -> List[Dict]:
        import pytz
        london = pytz.timezone("Europe/London")
        today = datetime.now(london).date()
        result = []
        for i in range(days):
            d = today - timedelta(days=i)
            label = "TODAY" if i == 0 else "YESTERDAY" if i == 1 else d.strftime("%a %d %b").upper()
            result.append({"date": d.isoformat(), "label": label, "pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
        return result
