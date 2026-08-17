import json
import logging
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import schedule

from .config import Config
from .data.oanda_client import OandaClient, MockOandaClient
from .execution.order_manager import OrderManager
from .execution.trade_manager import TradeManager
from .strategy.ema_filter import calculate_ema, check_ema_filter
from .strategy.initial_balance import calculate_initial_balance
from .strategy.risk import (
    calculate_atr,
    calculate_position_size,
    calculate_trade_levels,
)
from .strategy.signals import detect_breakout
from .utils.time_utils import (
    is_session_end,
    now_london,
    session_end_utc_str,
)
from .utils.trade_log import log_trade, update_trade_outcomes

logger = logging.getLogger(__name__)


@dataclass
class DayState:
    """Resets each trading day per instrument."""

    date: str = ""
    ib_high: Optional[float] = None
    ib_low: Optional[float] = None
    ib_ready: bool = False
    daily_atr: Optional[float] = None
    ema: Optional[float] = None
    trades_today: int = 0
    long_placed: bool = False
    short_placed: bool = False

    # Prevent the same direction from being proposed repeatedly.
    long_proposed: bool = False
    short_proposed: bool = False

    session_closed: bool = False
    active_order_ids: List[str] = field(default_factory=list)
    last_orders: List[Dict] = field(default_factory=list)


class HelixAgent:
    """
    Helix FX Trading Agent.

    Runs on 30-minute candle closes and executes the
    Initial Balance breakout strategy.
    """

    def __init__(
        self,
        config: Config,
        mode: str = "paper",
        state_reporter=None,
    ):
        self.config = config
        self.mode = mode
        self.instruments = list(config.instruments)
        self.state_reporter = state_reporter

        if mode in ("live", "practice"):
            environment = "live" if mode == "live" else "practice"

            self.client = OandaClient(
                account_id=config.broker.account_id,
                api_key=config.broker.api_key,
                environment=environment,
            )

            if mode == "live":
                logger.warning(
                    "LIVE MODE ACTIVE - real orders will be placed"
                )
            else:
                logger.info(
                    "PRACTICE MODE - using real OANDA market data "
                    "and practice account"
                )

        else:
            self.client = MockOandaClient()
            logger.info("Paper mode - no real orders will be placed")

        self.order_mgr = OrderManager(
            self.client,
            mode=mode,
        )

        self.trade_mgr = TradeManager(self.client)

        self._states: Dict[str, DayState] = {}

        self._state_path = Path("state/helix_state.json")
        self._proposals_path = Path("state/proposals.json")

        self._load_state()

    # ------------------------------------------------------------------ #
    # Persistent state
    # ------------------------------------------------------------------ #

    def _load_state(self):
        """Load today's trading state from disk."""

        if not self._state_path.exists():
            logger.info(
                "No saved Helix state found - starting fresh"
            )
            return

        try:
            with self._state_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                saved = json.load(file)

            today = now_london().strftime("%Y-%m-%d")

            for instrument, data in saved.items():
                if data.get("date") != today:
                    continue

                self._states[instrument] = DayState(**data)

            if self._states:
                logger.info(
                    f"Loaded saved state for "
                    f"{len(self._states)} instrument(s)"
                )
            else:
                logger.info(
                    "Saved state is from a previous day - starting fresh"
                )

        except Exception as exc:
            logger.warning(
                f"Could not load saved Helix state: {exc}"
            )

    def _save_state(self):
        """Persist current trading state to disk."""

        try:
            self._state_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            saved = {
                instrument: asdict(state)
                for instrument, state in self._states.items()
            }

            temp_path = self._state_path.with_suffix(".tmp")

            with temp_path.open(
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    saved,
                    file,
                    indent=2,
                )

            temp_path.replace(self._state_path)

            logger.info("Helix state saved")

        except Exception as exc:
            logger.warning(
                f"Could not save Helix state: {exc}"
            )

    def _load_proposals(self) -> List[Dict]:
        """Load dashboard proposals from disk."""

        if not self._proposals_path.exists():
            return []

        try:
            with self._proposals_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)

            if isinstance(data, list):
                return data

            logger.warning(
                "proposals.json is not a list - ignoring contents"
            )
            return []

        except Exception as exc:
            logger.warning(
                f"Could not load proposals: {exc}"
            )
            return []

    def _save_proposals(self, proposals: List[Dict]):
        """Save the complete proposal list to disk."""

        try:
            self._proposals_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temp_path = self._proposals_path.with_suffix(".tmp")

            with temp_path.open(
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    proposals,
                    file,
                    indent=2,
                )

            temp_path.replace(self._proposals_path)

        except Exception as exc:
            logger.warning(
                f"Could not save proposals: {exc}"
            )

    def _save_proposal(self, proposal: Dict):
        """Save or replace a pending trade proposal."""

        try:
            proposals = self._load_proposals()

            proposals = [
                item
                for item in proposals
                if item.get("id") != proposal["id"]
            ]

            saved_proposal = dict(proposal)
            saved_proposal["status"] = "pending"

            proposals.append(saved_proposal)

            self._save_proposals(proposals)

            logger.info(
                f"[{proposal['instrument']}] "
                "Proposal saved for dashboard"
            )

        except Exception as exc:
            logger.warning(
                f"Could not save proposal: {exc}"
            )

    def _update_proposal_status(
        self,
        proposal_id: str,
        status: str,
        extra: Optional[Dict] = None,
    ):
        """Update the status of a proposal stored on disk."""

        proposals = self._load_proposals()

        changed = False

        for proposal in proposals:
            if proposal.get("id") == proposal_id:
                proposal["status"] = status

                if extra:
                    proposal.update(extra)

                changed = True
                break

        if changed:
            self._save_proposals(proposals)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self):
        logger.info("=" * 60)
        logger.info("  Helix FX Trading Agent")
        logger.info(f"  Mode:        {self.mode.upper()}")
        logger.info(
            f"  Instruments: {', '.join(self.instruments)}"
        )
        logger.info(
            "  Strategy:    Initial Balance Breakout (M30)"
        )
        logger.info("  Session:     06:00-18:00 UK time")
        logger.info("=" * 60)

        self.tick()

        schedule.every().hour.at(":00").do(self.tick)
        schedule.every().hour.at(":30").do(self.tick)

        logger.info(
            "Helix is live. Checking every 30 minutes. "
            "Ctrl+C to stop."
        )

        try:
            while True:
                schedule.run_pending()
                _time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Helix stopped by user.")

    def tick(self):
        now = now_london()

        logger.info(
            f"--- Helix tick @ "
            f"{now.strftime('%Y-%m-%d %H:%M')} London ---"
        )

        for instrument in self.instruments:
            try:
                self._process(
                    instrument,
                    now,
                )

            except Exception as exc:
                logger.exception(
                    f"[{instrument}] "
                    f"Unhandled error in tick: {exc}"
                )

        self._save_state()
        self._push_state()

    # ------------------------------------------------------------------ #
    # Per-instrument logic
    # ------------------------------------------------------------------ #

    def _get_state(
        self,
        instrument: str,
    ) -> DayState:

        today = now_london().strftime("%Y-%m-%d")

        state = self._states.get(instrument)

        if state is None or state.date != today:
            self._states[instrument] = DayState(
                date=today,
            )

            logger.info(
                f"[{instrument}] "
                f"New day state for {today}"
            )

        return self._states[instrument]

    def _process(
        self,
        instrument: str,
        now: datetime,
    ):
        state = self._get_state(instrument)

        if is_session_end(now):
            if not state.session_closed:
                self._close_session(
                    instrument,
                    state,
                )

            return

        if (
            not state.ib_ready
            and now.hour >= self.config.ib_end_hour
        ):
            self._build_ib(
                instrument,
                state,
            )

        if not state.ib_ready:
            logger.debug(
                f"[{instrument}] IB not yet ready"
            )
            return

        if (
            state.trades_today
            >= self.config.max_trades_per_day
        ):
            logger.debug(
                f"[{instrument}] "
                f"Max trades reached "
                f"({state.trades_today})"
            )
            return

        if self.state_reporter is not None:
            if not self.state_reporter.get().get(
                "trading_enabled",
                False,
            ):
                logger.debug(
                    f"[{instrument}] "
                    "Trading not enabled - "
                    "waiting for user to press Go"
                )
                return

        self._evaluate_signals(
            instrument,
            state,
            now,
        )

    # ------------------------------------------------------------------ #
    # IB construction
    # ------------------------------------------------------------------ #

    def _build_ib(
        self,
        instrument: str,
        state: DayState,
    ):
        candles_m30 = self.client.get_candles(
            instrument,
            granularity="M30",
            count=50,
        )

        ib = calculate_initial_balance(candles_m30)

        if not ib:
            logger.info(
                f"[{instrument}] "
                "IB candles not yet available"
            )
            return

        candles_d1 = self.client.get_candles(
            instrument,
            granularity="D",
            count=20,
        )

        atr = calculate_atr(
            candles_d1,
            period=self.config.atr_period,
        )

        state.ib_high = ib["high"]
        state.ib_low = ib["low"]
        state.daily_atr = atr
        state.ib_ready = True

        logger.info(
            f"[{instrument}] "
            f"IB ready - "
            f"High={state.ib_high:.5f}  "
            f"Low={state.ib_low:.5f}  "
            f"Daily ATR={state.daily_atr:.5f}"
        )

    # ------------------------------------------------------------------ #
    # Signal evaluation
    # ------------------------------------------------------------------ #

    def _evaluate_signals(
        self,
        instrument: str,
        state: DayState,
        now: datetime,
    ):
        candles_m30 = self.client.get_candles(
            instrument,
            granularity="M30",
            count=210,
        )

        if len(candles_m30) < 2:
            return

        complete = [
            candle
            for candle in candles_m30
            if candle.get("complete", True)
        ]

        if not complete:
            return

        state.ema = calculate_ema(
            complete,
            period=self.config.ema_period,
        )

        last_closed = complete[-1]

        direction = detect_breakout(
            last_closed,
            state.ib_high,
            state.ib_low,
        )

        if direction is None:
            return

        if (
            direction == "long"
            and (
                state.long_placed
                or state.long_proposed
            )
        ):
            logger.debug(
                f"[{instrument}] "
                "Long already proposed/placed today"
            )
            return

        if (
            direction == "short"
            and (
                state.short_placed
                or state.short_proposed
            )
        ):
            logger.debug(
                f"[{instrument}] "
                "Short already proposed/placed today"
            )
            return

        levels = calculate_trade_levels(
            direction=direction,
            ib_high=state.ib_high,
            ib_low=state.ib_low,
            daily_atr=state.daily_atr,
            atr_buffer_pct=self.config.atr_buffer_pct,
            min_rr=self.config.min_rr,
        )

        if levels is None:
            logger.info(
                f"[{instrument}] "
                f"Cannot compute valid levels for {direction}"
            )
            return

        if not check_ema_filter(
            state.ema,
            levels["entry"],
            levels["target"],
            direction,
        ):
            logger.info(
                f"[{instrument}] "
                f"{direction.upper()} trade skipped - "
                f"EMA-{self.config.ema_period} "
                f"({state.ema:.5f}) blocks path to target"
            )
            return

        balance = self.client.get_account_balance()
        budget = None

        if self.state_reporter is not None:
            reporter_state = self.state_reporter.get()

            budget = reporter_state.get(
                "daily_budget"
            )

            if budget and float(budget) > 0:
                balance = min(
                    balance,
                    float(budget),
                )

        units = calculate_position_size(
            entry=levels["entry"],
            stop_loss=levels["stop_loss"],
            account_balance=balance,
            risk_percent=self.config.risk_percent,
            direction=direction,
            instrument=instrument,
        )

        proposal_id = (
            f"{instrument}-"
            f"{direction}-"
            f"{state.date}"
        )

        units_abs = abs(int(units))
        entry_px = levels["entry"]
        sl_px = levels["stop_loss"]
        tp_px = levels["target"]

        is_usd_base = instrument.startswith("USD_")

        if is_usd_base:
            position_usd = float(units_abs)

            reward_usd = (
                units_abs
                * abs(tp_px - entry_px)
                / entry_px
            )

            risk_usd = (
                units_abs
                * abs(entry_px - sl_px)
                / entry_px
            )

        else:
            position_usd = (
                units_abs
                * entry_px
            )

            reward_usd = (
                units_abs
                * abs(tp_px - entry_px)
            )

            risk_usd = (
                units_abs
                * abs(entry_px - sl_px)
            )

        proposal = {
            "id": proposal_id,
            "instrument": instrument,
            "direction": direction,
            "entry": entry_px,
            "stop_loss": sl_px,
            "take_profit": tp_px,
            "risk": levels["risk"],
            "rr": levels["rr"],
            "units": units_abs,
            "signed_units": int(units),
            "position_usd": round(
                position_usd,
                2,
            ),
            "reward_usd": round(
                reward_usd,
                2,
            ),
            "risk_usd": round(
                risk_usd,
                2,
            ),
            "ema": state.ema,
            "ib_high": state.ib_high,
            "ib_low": state.ib_low,
            "proposed_at": now_london().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "gtd_time": session_end_utc_str(now),
            "daily_budget": (
                float(budget)
                if budget
                and float(budget) > 0
                else None
            ),
        }

        if (
            self.state_reporter is not None
            and hasattr(
                self.state_reporter,
                "add_proposal",
            )
        ):
            self.state_reporter.add_proposal(
                proposal
            )

        self._save_proposal(proposal)

        if direction == "long":
            state.long_proposed = True

        else:
            state.short_proposed = True

        logger.info(
            f"[{instrument}] "
            f"Trade PROPOSED - "
            f"{direction.upper()} | "
            f"Entry={levels['entry']:.5f} "
            f"SL={levels['stop_loss']:.5f} "
            f"TP={levels['target']:.5f} "
            f"Units={abs(units)} "
            f"R:R=1:{levels['rr']} "
            f"EMA={state.ema:.5f} "
            "- awaiting user approval"
        )

    # ------------------------------------------------------------------ #
    # Manual approval / rejection
    # ------------------------------------------------------------------ #

    def approve_proposal(
        self,
        proposal_id: str,
    ) -> Dict:

        if (
            self.state_reporter is None
            or not hasattr(
                self.state_reporter,
                "pop_proposal",
            )
        ):
            return {
                "ok": False,
                "error": "State reporter not available",
            }

        proposal = self.state_reporter.pop_proposal(
            proposal_id
        )

        if proposal is None:
            return {
                "ok": False,
                "error": (
                    "Proposal not found "
                    "or already handled"
                ),
            }

        instrument = proposal["instrument"]
        direction = proposal["direction"]

        state = self._get_state(instrument)

        if (
            state.trades_today
            >= self.config.max_trades_per_day
        ):
            return {
                "ok": False,
                "error": (
                    "Max trades for the day "
                    "already reached"
                ),
            }

        if (
            direction == "long"
            and state.long_placed
        ):
            return {
                "ok": False,
                "error": (
                    "A long is already live for "
                    "this instrument today"
                ),
            }

        if (
            direction == "short"
            and state.short_placed
        ):
            return {
                "ok": False,
                "error": (
                    "A short is already live for "
                    "this instrument today"
                ),
            }

        order_id = self.order_mgr.place_stop_order(
            instrument=instrument,
            direction=direction,
            entry=proposal["entry"],
            stop_loss=proposal["stop_loss"],
            take_profit=proposal["take_profit"],
            units=proposal["signed_units"],
            gtd_time=proposal["gtd_time"],
        )

        if not order_id:
            self.state_reporter.add_proposal(
                proposal
            )

            return {
                "ok": False,
                "error": (
                    "Broker rejected the order "
                    "- see logs"
                ),
            }

        state.trades_today += 1
        state.active_order_ids.append(order_id)

        if direction == "long":
            state.long_placed = True

        else:
            state.short_placed = True

        state.last_orders.append(
            {
                "id": order_id,
                "instrument": instrument,
                "direction": direction,
                "entry": f"{proposal['entry']:.5f}",
                "stop_loss": f"{proposal['stop_loss']:.5f}",
                "take_profit": f"{proposal['take_profit']:.5f}",
                "risk": f"{proposal['risk']:.5f}",
                "units": proposal["units"],
            }
        )

        try:
            raw_balance = (
                self.client.get_account_balance()
            )

            log_trade(
                order_id=order_id,
                instrument=instrument,
                direction=direction,
                levels={
                    "entry": proposal["entry"],
                    "stop_loss": proposal["stop_loss"],
                    "target": proposal["take_profit"],
                    "risk": proposal["risk"],
                    "rr": proposal["rr"],
                },
                units=proposal["signed_units"],
                account_balance=raw_balance,
                daily_budget=proposal.get(
                    "daily_budget"
                ),
            )

        except Exception as exc:
            logger.warning(
                f"Post-approval logging failed: {exc}"
            )

        self._update_proposal_status(
            proposal_id,
            "placed",
            {
                "order_id": order_id,
                "placed_at": now_london().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            },
        )

        logger.info(
            f"[{instrument}] APPROVED - "
            f"{direction.upper()} "
            f"order {order_id} "
            f"Entry={proposal['entry']:.5f} "
            f"SL={proposal['stop_loss']:.5f} "
            f"TP={proposal['take_profit']:.5f} "
            f"Units={proposal['units']}"
        )

        self._save_state()
        self._push_state()

        return {
            "ok": True,
            "order_id": order_id,
        }

    def reject_proposal(
        self,
        proposal_id: str,
    ) -> Dict:

        if (
            self.state_reporter is None
            or not hasattr(
                self.state_reporter,
                "pop_proposal",
            )
        ):
            return {
                "ok": False,
                "error": "State reporter not available",
            }

        proposal = self.state_reporter.pop_proposal(
            proposal_id
        )

        if proposal is None:
            return {
                "ok": False,
                "error": "Proposal not found",
            }

        self._update_proposal_status(
            proposal_id,
            "rejected",
            {
                "rejected_at": now_london().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            },
        )

        logger.info(
            f"[{proposal['instrument']}] "
            f"REJECTED - "
            f"{proposal['direction'].upper()} "
            "proposal dismissed by user"
        )

        self._save_state()
        self._push_state()

        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Session close
    # ------------------------------------------------------------------ #

    def _close_session(
        self,
        instrument: str,
        state: DayState,
    ):
        logger.info(
            f"[{instrument}] "
            "18:00 UK reached - "
            "cancelling pending orders"
        )

        self.trade_mgr.cancel_all_pending(
            instrument,
            self.order_mgr,
        )

        state.session_closed = True

        if (
            self.state_reporter is not None
            and hasattr(
                self.state_reporter,
                "list_proposals",
            )
        ):
            for proposal in (
                self.state_reporter.list_proposals()
            ):
                if (
                    proposal["instrument"]
                    == instrument
                ):
                    self.state_reporter.pop_proposal(
                        proposal["id"]
                    )

    # ------------------------------------------------------------------ #
    # Dashboard state reporting
    # ------------------------------------------------------------------ #

    def _push_state(self):
        if self.state_reporter is None:
            return

        try:
            instruments_state = {}
            all_orders = []

            for instrument in self.instruments:
                state = self._states.get(
                    instrument
                )

                if state is None:
                    instruments_state[instrument] = {
                        "ib_high": None,
                        "ib_low": None,
                        "ib_ready": False,
                        "daily_atr": None,
                        "ema": None,
                        "trades_today": 0,
                        "long_placed": False,
                        "short_placed": False,
                        "long_proposed": False,
                        "short_proposed": False,
                        "session_closed": False,
                    }

                else:
                    instruments_state[instrument] = {
                        "ib_high": state.ib_high,
                        "ib_low": state.ib_low,
                        "ib_ready": state.ib_ready,
                        "daily_atr": state.daily_atr,
                        "ema": state.ema,
                        "trades_today": state.trades_today,
                        "long_placed": state.long_placed,
                        "short_placed": state.short_placed,
                        "long_proposed": state.long_proposed,
                        "short_proposed": state.short_proposed,
                        "session_closed": state.session_closed,
                    }

                    all_orders.extend(
                        state.last_orders
                    )

            balance = (
                self.client.get_account_balance()
            )

            try:
                pnl_days = (
                    self.client.get_pnl_by_day(
                        days=3
                    )
                )

            except Exception as exc:
                logger.warning(
                    f"P&L fetch failed: {exc}"
                )
                pnl_days = []

            try:
                closed = (
                    self.client.get_closed_trades()
                )

                update_trade_outcomes(
                    closed
                )

            except Exception as exc:
                logger.warning(
                    f"Trade outcome update failed: {exc}"
                )

            if self.mode == "live":
                oanda_env = "live"

            elif self.mode == "practice":
                oanda_env = "practice"

            else:
                oanda_env = "paper"

            self.state_reporter.update(
                {
                    "mode": self.mode,
                    "oanda_env": oanda_env,
                    "last_tick": (
                        now_london().strftime(
                            "%H:%M:%S"
                        )
                    ),
                    "account_balance": balance,
                    "max_trades": (
                        self.config.max_trades_per_day
                    ),
                    "risk_percent": (
                        self.config.risk_percent
                    ),
                    "instruments": instruments_state,
                    "orders": all_orders,
                    "pnl_days": pnl_days,
                }
            )

        except Exception as exc:
            logger.warning(
                f"State push failed: {exc}"
            )