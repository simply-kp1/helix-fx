"""OANDA historical candle downloader with disk cache."""
import json
import logging
import os
import time as _time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable

import pytz

from ..utils.paths import backtest_cache_dir

logger = logging.getLogger(__name__)


def _cache_path(instrument: str, granularity: str, from_dt: datetime, to_dt: datetime) -> str:
    fname = f"{instrument}_{granularity}_{from_dt.strftime('%Y%m%d')}_{to_dt.strftime('%Y%m%d')}.json"
    return os.path.join(backtest_cache_dir(), fname)


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def _parse_time(candle_time: str) -> datetime:
    return pytz.utc.localize(datetime.strptime(candle_time[:19], "%Y-%m-%dT%H:%M:%S"))


def build_data_client() -> Optional[object]:
    """Return a live OandaClient using env credentials, or None if unavailable."""
    account_id = os.environ.get("OANDA_ACCOUNT_ID")
    api_key = os.environ.get("OANDA_API_KEY")
    env = os.environ.get("OANDA_ENVIRONMENT", "practice")
    if not account_id or not api_key:
        return None
    try:
        from ..data.oanda_client import OandaClient
        return OandaClient(account_id=account_id, api_key=api_key, environment=env)
    except Exception as exc:
        logger.error(f"Could not build OANDA data client: {exc}")
        return None


def fetch_candles(
    client,
    instrument: str,
    granularity: str,
    from_dt: datetime,
    to_dt: datetime,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    """Fetch OANDA candles between from_dt and to_dt, paginated + disk-cached.

    OANDA's /candles endpoint caps count at 5000, so we page through the range
    using each batch's last candle time as the next `from`.
    """
    path = _cache_path(instrument, granularity, from_dt, to_dt)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                candles = json.load(f)
            if progress_cb:
                progress_cb(f"{instrument} {granularity}: {len(candles)} candles from cache")
            return candles
        except Exception as exc:
            logger.warning(f"Cache read failed for {path}: {exc}; refetching")

    from oandapyV20.endpoints.instruments import InstrumentsCandles

    all_candles: List[Dict] = []
    cursor = from_dt
    batch = 0
    while cursor < to_dt:
        batch += 1
        params = {
            "from": _to_rfc3339(cursor),
            "count": 5000,
            "granularity": granularity,
            "price": "M",
        }
        try:
            req = InstrumentsCandles(instrument=instrument, params=params)
            client.api.request(req)
            candles = req.response.get("candles", [])
        except Exception as exc:
            logger.error(f"Fetch batch {batch} failed for {instrument} {granularity}: {exc}")
            raise

        if not candles:
            break

        # Trim any candles past our target end date
        candles = [c for c in candles if _parse_time(c["time"]) <= to_dt]
        if not candles:
            break

        # Skip already-collected candles (first candle of new batch may equal cursor)
        if all_candles and candles[0]["time"] == all_candles[-1]["time"]:
            candles = candles[1:]
        if not candles:
            break  # nothing new after dedupe → we've caught up
        all_candles.extend(candles)

        if progress_cb:
            progress_cb(
                f"{instrument} {granularity}: batch {batch}, {len(all_candles):,} total"
            )

        last_time = _parse_time(candles[-1]["time"])
        if last_time <= cursor:
            break  # no forward progress → stop
        cursor = last_time + timedelta(seconds=1)

        _time.sleep(0.1)  # gentle rate-limit

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(all_candles, f)
    except Exception as exc:
        logger.warning(f"Cache write failed for {path}: {exc}")

    return all_candles
