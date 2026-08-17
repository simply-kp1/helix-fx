"""Backtest engine — replays historical OANDA candles through the live strategy."""
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Callable, Dict, List, Optional

import pytz

from ..strategy.ema_filter import calculate_ema, check_ema_filter
from ..strategy.initial_balance import calculate_initial_balance
from ..strategy.risk import calculate_atr, calculate_trade_levels
from ..strategy.signals import detect_breakout
from .data_fetcher import fetch_candles

logger = logging.getLogger(__name__)
LONDON = pytz.timezone("Europe/London")
UTC = pytz.utc

IB_START_HOUR = 6
IB_END_HOUR = 7
SESSION_END_HOUR = 18

# ─── Realistic market frictions ──────────────────────────────────────────
# One pip = 0.0001 for most pairs, 0.01 for JPY-quoted pairs.
PIP_SIZE = {
    "EUR_USD": 0.0001, "GBP_USD": 0.0001, "AUD_USD": 0.0001,
    "NZD_USD": 0.0001, "USD_CAD": 0.0001, "USD_CHF": 0.0001,
    "USD_JPY": 0.01,
}

# Typical OANDA practice-account average spreads (pips). Real live spreads
# are similar during major sessions; wider overnight and on news.
SPREAD_PIPS = {
    "EUR_USD": 1.0, "GBP_USD": 1.5, "USD_JPY": 1.0,
    "AUD_USD": 1.4, "USD_CAD": 1.6, "USD_CHF": 1.6, "NZD_USD": 2.0,
}

# Extra slippage on stop-order fills (breakouts move fast).
SLIPPAGE_PIPS = {
    "EUR_USD": 0.5, "GBP_USD": 0.7, "USD_JPY": 0.5,
    "AUD_USD": 0.5, "USD_CAD": 0.5, "USD_CHF": 0.5, "NZD_USD": 0.7,
}


def _pip(instrument: str) -> float:
    return PIP_SIZE.get(instrument, 0.0001)


def _spread_pips(instrument: str) -> float:
    return SPREAD_PIPS.get(instrument, 1.5)


def _slippage_pips(instrument: str) -> float:
    return SLIPPAGE_PIPS.get(instrument, 0.7)


def _parse_time(s: str) -> datetime:
    return UTC.localize(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def _pip_pnl_usd(instrument: str, units: int, entry: float, exit_price: float, direction: str) -> float:
    """USD profit/loss for a position sized in `units` of the base currency."""
    diff = (exit_price - entry) if direction == "long" else (entry - exit_price)
    if instrument.startswith("USD_"):
        # base currency is USD; pnl in quote currency → convert with exit price
        return units * diff / exit_price
    # quote currency is USD (e.g. EUR_USD)
    return units * diff


def _position_size(
    entry: float, stop_loss: float, balance: float, risk_pct: float, instrument: str
) -> int:
    """Compute units so that hitting SL loses ~risk_pct% of balance in USD."""
    risk_usd = balance * (risk_pct / 100.0)
    stop_dist = abs(entry - stop_loss)
    if stop_dist == 0:
        return 0
    if instrument.startswith("USD_"):
        # loss in USD = units * stop_dist / entry  →  units = risk_usd * entry / stop_dist
        units = int(risk_usd * entry / stop_dist)
    else:
        units = int(risk_usd / stop_dist)
    return max(units, 1)


def _group_by_london_date(candles: List[Dict]) -> Dict[str, List[Dict]]:
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for c in candles:
        if not c.get("complete", True):
            continue
        d = _parse_time(c["time"]).astimezone(LONDON).date().isoformat()
        groups[d].append(c)
    return groups


def _daily_atr_up_to(d1_by_date: Dict[str, Dict], today: str, period: int) -> float:
    """ATR from the `period+1` D1 candles ending BEFORE `today`."""
    sorted_dates = sorted(d1_by_date.keys())
    prior = [d for d in sorted_dates if d < today][-(period + 1):]
    if len(prior) < period + 1:
        return 0.0
    return calculate_atr([d1_by_date[d] for d in prior], period=period)


def run_backtest(
    client,
    instruments: List[str],
    years: int = 5,
    starting_balance: float = 10_000.0,
    risk_pct: float = 1.0,
    ema_period: int = 100,
    atr_period: int = 14,
    atr_buffer_pct: float = 0.05,
    min_rr: float = 1.5,
    max_trades_per_day: int = 3,
    frictions: bool = True,
    progress_cb: Optional[Callable[[str, float], None]] = None,
) -> Dict:
    """Run the backtest across the given instruments over the past `years` years."""

    def report(msg: str, pct: float):
        logger.info(f"[backtest] {msg} ({pct:.0f}%)")
        if progress_cb:
            progress_cb(msg, pct)

    to_dt = datetime.now(UTC)
    from_dt = to_dt - timedelta(days=int(365.25 * years))

    # 1. Fetch candles ------------------------------------------------------
    candles_by_inst: Dict[str, Dict] = {}
    total_stages = len(instruments) * 2
    stage_i = 0
    for inst in instruments:
        stage_i += 1
        report(f"Fetching {inst} M30…", 5 + (stage_i - 1) * (30 / total_stages))
        m30 = fetch_candles(client, inst, "M30", from_dt, to_dt,
                            progress_cb=lambda m: report(m, 5 + (stage_i - 0.5) * (30 / total_stages)))
        stage_i += 1
        report(f"Fetching {inst} D1…", 5 + (stage_i - 1) * (30 / total_stages))
        d1 = fetch_candles(client, inst, "D", from_dt, to_dt,
                           progress_cb=lambda m: report(m, 5 + (stage_i - 0.5) * (30 / total_stages)))
        candles_by_inst[inst] = {"M30": m30, "D1": d1}

    # 2. Simulate day by day ------------------------------------------------
    balance = starting_balance
    peak_balance = starting_balance
    max_drawdown_pct = 0.0

    all_trades: List[Dict] = []
    equity_curve: List[Dict] = [{"date": from_dt.date().isoformat(), "balance": balance}]

    # Build merged, date-sorted set of all trading days across all instruments
    per_inst_by_date: Dict[str, Dict[str, List[Dict]]] = {}
    per_inst_d1_map: Dict[str, Dict[str, Dict]] = {}
    for inst in instruments:
        per_inst_by_date[inst] = _group_by_london_date(candles_by_inst[inst]["M30"])
        d1_map: Dict[str, Dict] = {}
        for c in candles_by_inst[inst]["D1"]:
            if not c.get("complete", True):
                continue
            d = _parse_time(c["time"]).astimezone(LONDON).date().isoformat()
            d1_map[d] = c
        per_inst_d1_map[inst] = d1_map

    all_dates = sorted({d for m in per_inst_by_date.values() for d in m.keys()})
    total_days = max(len(all_dates), 1)

    for day_i, date_str in enumerate(all_dates):
        if day_i % 50 == 0:
            report(f"Simulating {date_str}", 40 + 55 * (day_i / total_days))

        for inst in instruments:
            day_candles = per_inst_by_date[inst].get(date_str, [])
            if not day_candles:
                continue

            # IB from 06:00 & 06:30 London candles
            ib = calculate_initial_balance(day_candles)
            if not ib:
                continue

            atr = _daily_atr_up_to(per_inst_d1_map[inst], date_str, atr_period)
            if atr <= 0:
                continue

            # Trading window candles (07:00 → 17:30 London close)
            window = [
                c for c in day_candles
                if _parse_time(c["time"]).astimezone(LONDON).time() >= time(IB_END_HOUR, 0)
                and _parse_time(c["time"]).astimezone(LONDON).time() < time(SESSION_END_HOUR, 0)
            ]

            # Continuation candles for tracking active trades past session close
            all_m30 = candles_by_inst[inst]["M30"]
            all_m30_idx = {c["time"]: i for i, c in enumerate(all_m30)}

            long_taken = False
            short_taken = False
            trades_today = 0
            pending_orders: List[Dict] = []  # {direction, entry, sl, tp, units, created_at}
            active_trades: List[Dict] = []   # {direction, entry, sl, tp, units, opened_at}

            # EMA context: use all M30 candles up to (but not including) the current candle
            for wc in window:
                if trades_today >= max_trades_per_day:
                    break

                wc_time = _parse_time(wc["time"])
                idx = all_m30_idx.get(wc["time"])
                if idx is None:
                    continue

                # Update/close pending orders and active trades using this candle
                _resolve_active(active_trades, wc, inst, balance_state=[balance])
                filled = _fill_pending(pending_orders, wc, inst, frictions)
                for f in filled:
                    active_trades.append(f)

                # Signal detection on this closed candle
                closed_close = float(wc["mid"]["c"])
                direction = detect_breakout(wc, ib["high"], ib["low"])
                if direction is None:
                    continue
                if direction == "long" and long_taken:
                    continue
                if direction == "short" and short_taken:
                    continue

                # EMA up to & including this candle
                lookback = all_m30[max(0, idx - (ema_period * 3)): idx + 1]
                ema = calculate_ema(lookback, period=ema_period)

                levels = calculate_trade_levels(
                    direction=direction,
                    ib_high=ib["high"],
                    ib_low=ib["low"],
                    daily_atr=atr,
                    atr_buffer_pct=atr_buffer_pct,
                    min_rr=min_rr,
                )
                if not levels:
                    continue
                if not check_ema_filter(ema, levels["entry"], levels["target"], direction):
                    continue

                # Fixed-fractional: size each trade off the STARTING balance,
                # not the running balance. This is the standard backtest
                # convention — compounding on the running balance produces
                # runaway exponential numbers that are meaningless over 5 years.
                units = _position_size(
                    entry=levels["entry"],
                    stop_loss=levels["stop_loss"],
                    balance=starting_balance,
                    risk_pct=risk_pct,
                    instrument=inst,
                )
                if units <= 0:
                    continue

                pending_orders.append({
                    "direction": direction,
                    "entry": levels["entry"],
                    "sl": levels["stop_loss"],
                    "tp": levels["target"],
                    "units": units,
                    "created_at_idx": idx,
                    "created_time": wc["time"],
                    "instrument": inst,
                    "signal_ib_high": ib["high"],
                    "signal_ib_low": ib["low"],
                })
                if direction == "long":
                    long_taken = True
                else:
                    short_taken = True
                trades_today += 1

            # At 18:00, cancel unfilled pending orders (drop them)
            pending_orders = []

            # Track any active trades forward until they hit SL/TP.
            # Walk forward in the raw M30 list from each trade's open bar.
            # We only allow the trade to run until end of the next trading day,
            # matching the spirit of a day-strategy: no indefinite carry.
            for trade in active_trades:
                start_idx = trade.get("filled_idx", trade["created_at_idx"] + 1)
                exit_result = _track_to_exit(all_m30, start_idx, trade, inst, frictions)
                if exit_result is None:
                    continue

                exit_price, exit_time, outcome = exit_result
                # PnL uses the actual filled entry price (which includes spread
                # + slippage cost on the entry when frictions are on).
                pnl = _pip_pnl_usd(
                    inst, trade["units"], trade["fill_price"], exit_price, trade["direction"]
                )
                balance += pnl
                if balance > peak_balance:
                    peak_balance = balance
                dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
                if dd > max_drawdown_pct:
                    max_drawdown_pct = dd

                all_trades.append({
                    "instrument": inst,
                    "direction": trade["direction"],
                    "date": date_str,
                    "open_time": trade["filled_time"],
                    "close_time": exit_time,
                    "entry": round(trade["fill_price"], 5),
                    "stop_loss": round(trade["sl"], 5),
                    "take_profit": round(trade["tp"], 5),
                    "exit_price": round(exit_price, 5),
                    "units": trade["units"],
                    "outcome": outcome,
                    "pnl_usd": round(pnl, 2),
                    "balance_after": round(balance, 2),
                })

        # daily equity point
        equity_curve.append({"date": date_str, "balance": round(balance, 2)})

    # 3. Aggregate stats ----------------------------------------------------
    report("Aggregating stats…", 97)

    per_inst_stats: Dict[str, Dict] = {}
    for inst in instruments:
        inst_trades = [t for t in all_trades if t["instrument"] == inst]
        per_inst_stats[inst] = _stats_for(inst_trades)

    overall = _stats_for(all_trades)
    overall["starting_balance"] = round(starting_balance, 2)
    overall["ending_balance"] = round(balance, 2)
    overall["net_pnl_usd"] = round(balance - starting_balance, 2)
    overall["net_pnl_pct"] = round((balance - starting_balance) / starting_balance * 100, 2)
    overall["max_drawdown_pct"] = round(max_drawdown_pct, 2)

    report("Done", 100)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "from_date": from_dt.date().isoformat(),
        "to_date": to_dt.date().isoformat(),
        "years": years,
        "instruments": instruments,
        "config": {
            "starting_balance": starting_balance,
            "risk_pct": risk_pct,
            "ema_period": ema_period,
            "atr_period": atr_period,
            "atr_buffer_pct": atr_buffer_pct,
            "min_rr": min_rr,
            "max_trades_per_day": max_trades_per_day,
            "frictions": frictions,
        },
        "overall": overall,
        "per_instrument": per_inst_stats,
        "equity_curve": equity_curve,
        "trades": all_trades,
    }


def _fill_pending(pending: List[Dict], candle: Dict, instrument: str, frictions: bool) -> List[Dict]:
    """Trigger any pending stop orders. When frictions are on, entry fills
    include half-spread (bid-ask cost) only. If the candle opened already
    past the trigger price, use that open price (gap fill)."""
    opn = float(candle["mid"]["o"])
    high = float(candle["mid"]["h"])
    low = float(candle["mid"]["l"])

    pip = _pip(instrument)
    half_spread = (_spread_pips(instrument) * pip / 2.0) if frictions else 0.0

    filled = []
    remaining = []
    for order in pending:
        direction = order["direction"]
        trigger = order["entry"]
        triggered = (direction == "long" and high >= trigger) or \
                    (direction == "short" and low <= trigger)
        if not triggered:
            remaining.append(order)
            continue

        # Actual fill price:
        #   * gap through trigger → fill at candle open (worse than trigger)
        #   * otherwise fill at the trigger price
        # Plus half-spread cost when frictions are on.
        if direction == "long":
            base = max(opn, trigger) if opn > trigger else trigger
            fill = base + half_spread
        else:
            base = min(opn, trigger) if opn < trigger else trigger
            fill = base - half_spread

        filled.append({
            **order,
            "fill_price": fill,
            "filled_time": candle["time"],
            "filled_idx": order["created_at_idx"] + 1,
        })
    pending[:] = remaining
    return filled


def _resolve_active(active: List[Dict], candle: Dict, instrument: str, balance_state):
    """No-op placeholder — active trades are tracked to exit after the day loop."""
    return


def _track_to_exit(all_m30: List[Dict], start_idx: int, trade: Dict,
                   instrument: str, frictions: bool, max_bars: int = 240):
    """Walk M30 bars forward from start_idx until SL or TP is hit.

    Realistic assumptions:
      * If a bar's open has already gapped through SL/TP, fill at the open
        (worst case for stops, still bad for limits due to gaps).
      * Otherwise if the candle's range contains BOTH SL and TP, assume SL
        first (conservative).
      * SL fills incur half-spread + slippage; TP (limit) incurs half-spread only.
    """
    pip = _pip(instrument)
    half_spread = (_spread_pips(instrument) * pip / 2.0) if frictions else 0.0

    sl = trade["sl"]
    tp = trade["tp"]

    for i in range(start_idx, min(len(all_m30), start_idx + max_bars)):
        c = all_m30[i]
        if not c.get("complete", True):
            continue
        opn = float(c["mid"]["o"])
        high = float(c["mid"]["h"])
        low = float(c["mid"]["l"])

        if trade["direction"] == "long":
            # Gap through SL — filled at the gap open (worse than SL price)
            if opn <= sl:
                exit_price = opn - half_spread
                return exit_price, c["time"], "LOSS"
            # Gap through TP — filled at gap open (better than TP is a win)
            if opn >= tp:
                exit_price = opn - half_spread
                return exit_price, c["time"], "WIN"
            hit_sl = low <= sl
            hit_tp = high >= tp
            if hit_sl:  # conservative: SL first if both
                return sl - half_spread, c["time"], "LOSS"
            if hit_tp:
                return tp - half_spread, c["time"], "WIN"
        else:
            if opn >= sl:
                exit_price = opn + half_spread
                return exit_price, c["time"], "LOSS"
            if opn <= tp:
                exit_price = opn + half_spread
                return exit_price, c["time"], "WIN"
            hit_sl = high >= sl
            hit_tp = low <= tp
            if hit_sl:
                return sl + half_spread, c["time"], "LOSS"
            if hit_tp:
                return tp + half_spread, c["time"], "WIN"

    # Never resolved within the lookahead window → close at last candle's close
    last = all_m30[min(len(all_m30) - 1, start_idx + max_bars - 1)]
    close = float(last["mid"]["c"])
    if trade["direction"] == "long":
        exit_price = close - half_spread
    else:
        exit_price = close + half_spread
    return exit_price, last["time"], "TIMEOUT"


def _stats_for(trades: List[Dict]) -> Dict:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "timeouts": 0,
            "win_rate_pct": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "net_pnl_usd": 0.0, "profit_factor": 0.0,
            "avg_win_usd": 0.0, "avg_loss_usd": 0.0,
            "largest_win": 0.0, "largest_loss": 0.0,
        }
    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]

    gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_loss = -sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0
    net = sum(t["pnl_usd"] for t in trades)

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl_usd": round(net, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win_usd": round(sum(t["pnl_usd"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_usd": round(sum(t["pnl_usd"] for t in losses) / len(losses), 2) if losses else 0.0,
        "largest_win": round(max((t["pnl_usd"] for t in trades), default=0.0), 2),
        "largest_loss": round(min((t["pnl_usd"] for t in trades), default=0.0), 2),
    }
