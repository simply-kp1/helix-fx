import json
import logging
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import schedule

from .config import Config
from .data.oanda_client import OandaClient, MockOandaClient
from .execution.order_manager import OrderManager, PRICE_DECIMALS as _PRICE_DECIMALS
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


def _price_fmt(instrument: str, price: float) -> str:
    decimals = _PRICE_DECIMALS.get(instrument, 5)
    return f"{price:.{decimals}f}"


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

        # --------------------------------------------------------------
        # Broker client
        # --------------------------------------------------------------

        if mode in ("live", "practice"):
            environment = "live" if mode == "live" else "practice"

            self.client = OandaClient(
                account_id=config.broker.account_id,
                api_key=config.broker.api_key,
                environment=environment,
            )

            if mode == "live":
                logger.warning(
                    "LIVE MODE ACTIVE - real orders could be placed"
                )
            else:
                logger.info(
                    "PRACTICE MODE - using real OANDA market data "
                    "and practice account"
                )

        else:
            self.client = MockOandaClient()
            logger.info(
                "Paper mode - no real orders will be placed"
            )

        self.order_mgr = OrderManager(
            self.client,
            mode=mode,
        )

        self.trade_mgr = TradeManager(self.client)

        # --------------------------------------------------------------
        # Persistent files
        # --------------------------------------------------------------

        self._state_path = Path(
            "state/helix_state.json"
        )

        self._proposals_path = Path(
            "state/proposals.json"
        )

        self._control_path = Path(
            "state/control.json"
        )

        self._states: Dict[str, DayState] = {}

        self._load_state()

    # ==================================================================
    # JSON helpers
    # ==================================================================

    @staticmethod
    def _atomic_write_json(
        path: Path,
        data,
    ):
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_path = path.with_suffix(".tmp")

        with temp_path.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                indent=2,
            )

        temp_path.replace(path)

    # ==================================================================
    # Trading state
    # ==================================================================

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

            today = now_london().strftime(
                "%Y-%m-%d"
            )

            for instrument, data in saved.items():

                if data.get("date") != today:
                    continue

                self._states[instrument] = (
                    DayState(**data)
                )

            if self._states:

                logger.info(
                    f"Loaded saved state for "
                    f"{len(self._states)} instrument(s)"
                )

            else:

                logger.info(
                    "Saved state is from a previous day "
                    "- starting fresh"
                )

        except Exception as exc:

            logger.warning(
                f"Could not load saved Helix state: "
                f"{exc}"
            )

    def _save_state(self):
        """Persist trading state to disk."""

        try:

            saved = {
                instrument: asdict(state)
                for instrument, state
                in self._states.items()
            }

            self._atomic_write_json(
                self._state_path,
                saved,
            )

            logger.info(
                "Helix state saved"
            )

        except Exception as exc:

            logger.warning(
                f"Could not save Helix state: "
                f"{exc}"
            )

    # ==================================================================
    # Dashboard control file
    # ==================================================================

    def _default_control(self) -> Dict:

        return {
            "trading_enabled": False,
            "daily_budget": None,
            "account_balance": None,
            "last_tick": None,
            "max_trades": (
                self.config.max_trades_per_day
            ),
            "risk_percent": (
                self.config.risk_percent
            ),
            "pnl_days": [],
        }

    def _load_control(self) -> Dict:
        """Read dashboard GO/STOP and budget state."""

        default = self._default_control()

        if not self._control_path.exists():
            return default

        try:

            with self._control_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                saved = json.load(file)

            if isinstance(saved, dict):
                default.update(saved)

            return default

        except Exception as exc:

            logger.warning(
                f"Could not load control state: "
                f"{exc}"
            )

            return default

    def _save_control(
        self,
        control: Dict,
    ):

        try:

            self._atomic_write_json(
                self._control_path,
                control,
            )

        except Exception as exc:

            logger.warning(
                f"Could not save control state: "
                f"{exc}"
            )

    # ==================================================================
    # Proposal storage
    # ==================================================================

    def _load_proposals(self) -> List[Dict]:

        if not self._proposals_path.exists():
            return []

        try:

            with self._proposals_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                proposals = json.load(file)

            if isinstance(proposals, list):
                return proposals

            return []

        except Exception as exc:

            logger.warning(
                f"Could not load proposals: "
                f"{exc}"
            )

            return []

    def _save_proposals(
        self,
        proposals: List[Dict],
    ):

        try:

            self._atomic_write_json(
                self._proposals_path,
                proposals,
            )

        except Exception as exc:

            logger.warning(
                f"Could not save proposals: "
                f"{exc}"
            )

    def _save_proposal(
        self,
        proposal: Dict,
    ):
        """
        Add a new pending proposal for the dashboard.
        """

        proposals = self._load_proposals()

        proposals = [
            existing
            for existing in proposals
            if existing.get("id")
            != proposal["id"]
        ]

        saved = dict(proposal)

        saved["status"] = "pending"

        proposals.append(saved)

        self._save_proposals(
            proposals
        )

        logger.info(
            f"[{proposal['instrument']}] "
            "Proposal saved for dashboard"
        )

    def _update_proposal(
        self,
        proposal_id: str,
        status: str,
        extra: Optional[Dict] = None,
    ):

        proposals = self._load_proposals()

        for proposal in proposals:

            if (
                proposal.get("id")
                != proposal_id
            ):
                continue

            proposal["status"] = status

            if extra:
                proposal.update(extra)

            break

        self._save_proposals(
            proposals
        )

    # ==================================================================
    # Approved proposals from dashboard
    # ==================================================================

    def _process_approved_proposals(
        self,
        now: datetime,
    ):
        """
        Look for proposals approved through the dashboard.

        IMPORTANT:
        GitHub-backed approvals are only allowed to place
        OANDA PRACTICE orders.

        Live-money orders are deliberately blocked here.
        """

        proposals = self._load_proposals()

        approved = [
            proposal
            for proposal in proposals
            if proposal.get("status")
            == "approved"
        ]

        if not approved:
            return

        # --------------------------------------------------------------
        # Safety block
        # --------------------------------------------------------------

        if self.mode != "practice":

            for proposal in approved:

                logger.error(
                    f"[{proposal.get('instrument')}] "
                    "Approved GitHub proposal blocked - "
                    "automatic dashboard approvals are "
                    "PRACTICE ONLY"
                )

                proposal["status"] = (
                    "blocked_non_practice"
                )

                proposal["error"] = (
                    "Dashboard approval processing "
                    "is restricted to OANDA practice mode"
                )

            self._save_proposals(
                proposals
            )

            return

        # --------------------------------------------------------------
        # GO / STOP check
        # --------------------------------------------------------------

        control = self._load_control()

        if not control.get(
            "trading_enabled",
            False,
        ):

            logger.info(
                "Approved proposal found but trading "
                "is currently STOPPED"
            )

            # Leave status as approved.
            # It can execute once the user presses GO.
            return

        # --------------------------------------------------------------
        # Session safety
        # --------------------------------------------------------------

        if is_session_end(now, self.config.session_end_hour):

            for proposal in approved:

                proposal["status"] = "expired"

                proposal["error"] = (
                    "Trading session had ended "
                    "before order placement"
                )

            self._save_proposals(
                proposals
            )

            return

        today = now.strftime(
            "%Y-%m-%d"
        )

        changed = False

        for proposal in proposals:

            if (
                proposal.get("status")
                != "approved"
            ):
                continue

            proposal_id = proposal.get("id")

            instrument = proposal.get(
                "instrument"
            )

            direction = proposal.get(
                "direction"
            )

            # ----------------------------------------------------------
            # Validate proposal
            # ----------------------------------------------------------

            if (
                instrument
                not in self.instruments
            ):

                proposal["status"] = "failed"

                proposal["error"] = (
                    "Unknown instrument"
                )

                changed = True

                continue

            proposed_at = str(
                proposal.get(
                    "proposed_at",
                    "",
                )
            )

            if (
                proposed_at
                and not proposed_at.startswith(
                    today
                )
            ):

                proposal["status"] = "expired"

                proposal["error"] = (
                    "Proposal belongs to "
                    "a previous trading day"
                )

                changed = True

                continue

            if direction not in (
                "long",
                "short",
            ):

                proposal["status"] = "failed"

                proposal["error"] = (
                    "Invalid trade direction"
                )

                changed = True

                continue

            state = self._get_state(
                instrument
            )

            # ----------------------------------------------------------
            # Daily trade limit
            # ----------------------------------------------------------

            if (
                state.trades_today
                >= self.config.max_trades_per_day
            ):

                proposal["status"] = "failed"

                proposal["error"] = (
                    "Maximum trades for today "
                    "already reached"
                )

                changed = True

                continue

            # ----------------------------------------------------------
            # Prevent duplicate direction
            # ----------------------------------------------------------

            if (
                direction == "long"
                and state.long_placed
            ):

                proposal["status"] = "failed"

                proposal["error"] = (
                    "Long order already placed "
                    "today"
                )

                changed = True

                continue

            if (
                direction == "short"
                and state.short_placed
            ):

                proposal["status"] = "failed"

                proposal["error"] = (
                    "Short order already placed "
                    "today"
                )

                changed = True

                continue

            # ----------------------------------------------------------
            # Staleness check: verify market hasn't moved past entry
            # ----------------------------------------------------------

            try:
                candles = self.client.get_candles(
                    instrument, granularity="M30", count=2
                )
                complete = [c for c in candles if c.get("complete", True)]
                if complete:
                    current_price = float(complete[-1]["mid"]["c"])
                    entry_price = float(proposal["entry"])
                    dec = _PRICE_DECIMALS.get(instrument, 5)
                    if direction == "long" and current_price >= entry_price:
                        proposal["status"] = "stale"
                        proposal["error"] = (
                            f"Market ({current_price:.{dec}f}) already at or above "
                            f"entry ({entry_price:.{dec}f}) — buy stop would be rejected"
                        )
                        logger.warning(
                            f"[{instrument}] Skipping stale LONG: "
                            f"market {current_price:.{dec}f} >= entry {entry_price:.{dec}f}"
                        )
                        changed = True
                        continue
                    elif direction == "short" and current_price <= entry_price:
                        proposal["status"] = "stale"
                        proposal["error"] = (
                            f"Market ({current_price:.{dec}f}) already at or below "
                            f"entry ({entry_price:.{dec}f}) — sell stop would be rejected"
                        )
                        logger.warning(
                            f"[{instrument}] Skipping stale SHORT: "
                            f"market {current_price:.{dec}f} <= entry {entry_price:.{dec}f}"
                        )
                        changed = True
                        continue
            except Exception as exc:
                logger.warning(
                    f"[{instrument}] Staleness check failed: {exc} — proceeding"
                )

            # ----------------------------------------------------------
            # Place OANDA PRACTICE order
            # ----------------------------------------------------------

            logger.info(
                f"[{instrument}] "
                f"Processing APPROVED "
                f"{direction.upper()} proposal"
            )

            order_id = (
                self.order_mgr.place_stop_order(
                    instrument=instrument,
                    direction=direction,
                    entry=float(
                        proposal["entry"]
                    ),
                    stop_loss=float(
                        proposal["stop_loss"]
                    ),
                    take_profit=float(
                        proposal["take_profit"]
                    ),
                    units=int(
                        proposal["signed_units"]
                    ),
                    gtd_time=proposal[
                        "gtd_time"
                    ],
                )
            )

            if not order_id:

                proposal["status"] = "failed"

                proposal["error"] = (
                    "OANDA rejected the order"
                )

                changed = True

                continue

            # ----------------------------------------------------------
            # Update trading state
            # ----------------------------------------------------------

            state.trades_today += 1

            state.active_order_ids.append(
                str(order_id)
            )

            if direction == "long":
                state.long_placed = True

            else:
                state.short_placed = True

            state.last_orders.append(
                {
                    "id": str(order_id),
                    "instrument": instrument,
                    "direction": direction,
                    "entry": _price_fmt(instrument, float(proposal["entry"])),
                    "stop_loss": _price_fmt(instrument, float(proposal["stop_loss"])),
                    "take_profit": _price_fmt(instrument, float(proposal["take_profit"])),
                    "risk": _price_fmt(instrument, float(proposal["risk"])),
                    "units": int(proposal["units"]),
                    "status": "pending",
                }
            )

            # ----------------------------------------------------------
            # Update proposal
            # ----------------------------------------------------------

            proposal["status"] = "placed"

            proposal["order_id"] = str(
                order_id
            )

            proposal["placed_at"] = (
                now_london().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )

            changed = True

            # ----------------------------------------------------------
            # Trade log
            # ----------------------------------------------------------

            try:

                raw_balance = (
                    self.client
                    .get_account_balance()
                )

                log_trade(
                    order_id=str(order_id),
                    instrument=instrument,
                    direction=direction,
                    levels={
                        "entry": float(
                            proposal["entry"]
                        ),
                        "stop_loss": float(
                            proposal["stop_loss"]
                        ),
                        "target": float(
                            proposal["take_profit"]
                        ),
                        "risk": float(
                            proposal["risk"]
                        ),
                        "rr": proposal["rr"],
                    },
                    units=int(
                        proposal["signed_units"]
                    ),
                    account_balance=raw_balance,
                    daily_budget=proposal.get(
                        "daily_budget"
                    ),
                )

            except Exception as exc:

                logger.warning(
                    f"Trade logging failed: "
                    f"{exc}"
                )

            logger.info(
                f"[{instrument}] "
                f"APPROVED PRACTICE ORDER PLACED "
                f"- {direction.upper()} "
                f"order={order_id}"
            )

        if changed:

            self._save_proposals(
                proposals
            )

            self._save_state()

    # ==================================================================
    # Public API
    # ==================================================================

    def run(self):

        logger.info(
            "=" * 60
        )

        logger.info(
            "  Helix FX Trading Agent"
        )

        logger.info(
            f"  Mode:        "
            f"{self.mode.upper()}"
        )

        logger.info(
            f"  Instruments: "
            f"{', '.join(self.instruments)}"
        )

        logger.info(
            "  Strategy:    "
            "Initial Balance Breakout (M30)"
        )

        logger.info(
            "  Session:     "
            f"{self.config.ib_start_hour:02d}:00-{self.config.session_end_hour:02d}:00 UK time"
        )

        logger.info(
            "=" * 60
        )

        self.tick()

        schedule.every().hour.at(
            ":00"
        ).do(self.tick)

        schedule.every().hour.at(
            ":30"
        ).do(self.tick)

        logger.info(
            "Helix is running. "
            "Checking every 30 minutes."
        )

        try:

            while True:

                schedule.run_pending()

                _time.sleep(1)

        except KeyboardInterrupt:

            logger.info(
                "Helix stopped by user."
            )

    def tick(self):

        now = now_london()

        logger.info(
            f"--- Helix tick @ "
            f"{now.strftime('%Y-%m-%d %H:%M')} "
            f"London ---"
        )

        # First process anything the user approved
        # through the dashboard.
        self._process_approved_proposals(
            now
        )

        # Then run normal market scanning.
        for instrument in self.instruments:

            try:

                self._process(
                    instrument,
                    now,
                )

            except Exception as exc:

                logger.exception(
                    f"[{instrument}] "
                    f"Unhandled error in tick: "
                    f"{exc}"
                )

        self._save_state()

        self._update_control_snapshot()

        self._push_state()

    # ==================================================================
    # Per-instrument processing
    # ==================================================================

    def _get_state(
        self,
        instrument: str,
    ) -> DayState:

        today = (
            now_london().strftime(
                "%Y-%m-%d"
            )
        )

        state = self._states.get(
            instrument
        )

        if (
            state is None
            or state.date != today
        ):

            self._states[instrument] = (
                DayState(
                    date=today,
                )
            )

            logger.info(
                f"[{instrument}] "
                f"New day state for "
                f"{today}"
            )

        return self._states[
            instrument
        ]

    def _process(
        self,
        instrument: str,
        now: datetime,
    ):

        state = self._get_state(
            instrument
        )

        # --------------------------------------------------------------
        # Session end
        # --------------------------------------------------------------

        if is_session_end(now, self.config.session_end_hour):

            if not state.session_closed:

                self._close_session(
                    instrument,
                    state,
                )

            return

        # --------------------------------------------------------------
        # Build Initial Balance
        # --------------------------------------------------------------

        if (
            not state.ib_ready
            and now.hour
            >= self.config.ib_end_hour
        ):

            self._build_ib(
                instrument,
                state,
            )

        if not state.ib_ready:

            logger.debug(
                f"[{instrument}] "
                "IB not yet ready"
            )

            return

        # --------------------------------------------------------------
        # Daily trade limit
        # --------------------------------------------------------------

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

        # --------------------------------------------------------------
        # Dashboard GO / STOP
        # --------------------------------------------------------------

        control = self._load_control()

        if not control.get(
            "trading_enabled",
            False,
        ):

            logger.debug(
                f"[{instrument}] "
                "Trading paused from dashboard"
            )

            return

        # --------------------------------------------------------------
        # Evaluate signals
        # --------------------------------------------------------------

        self._evaluate_signals(
            instrument,
            state,
            now,
            control,
        )

    # ==================================================================
    # Initial Balance
    # ==================================================================

    def _build_ib(
        self,
        instrument: str,
        state: DayState,
    ):

        candles_m30 = (
            self.client.get_candles(
                instrument,
                granularity="M30",
                count=50,
            )
        )

        ib = calculate_initial_balance(
            candles_m30,
            ib_start_hour=self.config.ib_start_hour,
        )

        if not ib:

            logger.info(
                f"[{instrument}] "
                "IB candles not yet available"
            )

            return

        candles_d1 = (
            self.client.get_candles(
                instrument,
                granularity="D",
                count=20,
            )
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
            f"High={state.ib_high:.5f} "
            f"Low={state.ib_low:.5f} "
            f"Daily ATR="
            f"{state.daily_atr:.5f}"
        )

    # ==================================================================
    # Signal evaluation
    # ==================================================================

    def _evaluate_signals(
        self,
        instrument: str,
        state: DayState,
        now: datetime,
        control: Dict,
    ):

        candles_m30 = (
            self.client.get_candles(
                instrument,
                granularity="M30",
                count=210,
            )
        )

        if len(candles_m30) < 2:
            return

        complete = [
            candle
            for candle in candles_m30
            if candle.get(
                "complete",
                True,
            )
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

        # --------------------------------------------------------------
        # Duplicate prevention
        # --------------------------------------------------------------

        if (
            direction == "long"
            and (
                state.long_placed
                or state.long_proposed
            )
        ):

            logger.debug(
                f"[{instrument}] "
                "Long already "
                "proposed/placed today"
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
                "Short already "
                "proposed/placed today"
            )

            return

        # --------------------------------------------------------------
        # Trade levels
        # --------------------------------------------------------------

        levels = calculate_trade_levels(
            direction=direction,
            ib_high=state.ib_high,
            ib_low=state.ib_low,
            daily_atr=state.daily_atr,
            atr_buffer_pct=(
                self.config.atr_buffer_pct
            ),
            min_rr=self.config.min_rr,
        )

        if levels is None:

            logger.info(
                f"[{instrument}] "
                "Cannot compute valid "
                f"levels for {direction}"
            )

            return

        # --------------------------------------------------------------
        # EMA filter
        # --------------------------------------------------------------

        if not check_ema_filter(
            state.ema,
            levels["entry"],
            levels["target"],
            direction,
        ):

            logger.info(
                f"[{instrument}] "
                f"{direction.upper()} "
                "trade skipped - "
                f"EMA-{self.config.ema_period} "
                f"({state.ema:.5f}) "
                "blocks path to target"
            )

            return

        # --------------------------------------------------------------
        # Account balance + daily budget
        # --------------------------------------------------------------

        balance = (
            self.client
            .get_account_balance()
        )

        budget = control.get(
            "daily_budget"
        )

        if (
            budget is not None
            and float(budget) > 0
        ):

            balance = min(
                balance,
                float(budget),
            )

        # --------------------------------------------------------------
        # Position sizing
        # --------------------------------------------------------------

        units = calculate_position_size(
            entry=levels["entry"],
            stop_loss=levels[
                "stop_loss"
            ],
            account_balance=balance,
            risk_percent=(
                self.config.risk_percent
            ),
            direction=direction,
            instrument=instrument,
        )

        proposal_id = (
            f"{instrument}-"
            f"{direction}-"
            f"{state.date}"
        )

        units_abs = abs(
            int(units)
        )

        entry_px = levels["entry"]
        sl_px = levels["stop_loss"]
        tp_px = levels["target"]

        is_usd_base = (
            instrument.startswith(
                "USD_"
            )
        )

        if is_usd_base:

            position_usd = float(
                units_abs
            )

            reward_usd = (
                units_abs
                * abs(
                    tp_px
                    - entry_px
                )
                / entry_px
            )

            risk_usd = (
                units_abs
                * abs(
                    entry_px
                    - sl_px
                )
                / entry_px
            )

        else:

            position_usd = (
                units_abs
                * entry_px
            )

            reward_usd = (
                units_abs
                * abs(
                    tp_px
                    - entry_px
                )
            )

            risk_usd = (
                units_abs
                * abs(
                    entry_px
                    - sl_px
                )
            )

        # --------------------------------------------------------------
        # Proposal
        # --------------------------------------------------------------

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
            "proposed_at": (
                now_london().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            ),
            "gtd_time": (
                session_end_utc_str(now, self.config.session_end_hour)
            ),
            "daily_budget": (
                float(budget)
                if (
                    budget is not None
                    and float(budget) > 0
                )
                else None
            ),
        }

        # Existing local dashboard support.
        if (
            self.state_reporter
            is not None
            and hasattr(
                self.state_reporter,
                "add_proposal",
            )
        ):

            self.state_reporter.add_proposal(
                proposal
            )

        # GitHub-backed dashboard support.
        self._save_proposal(
            proposal
        )

        if direction == "long":

            state.long_proposed = True

        else:

            state.short_proposed = True

        _dec = _PRICE_DECIMALS.get(instrument, 5)
        logger.info(
            f"[{instrument}] "
            f"Trade PROPOSED - "
            f"{direction.upper()} | "
            f"Entry={levels['entry']:.{_dec}f} "
            f"SL={levels['stop_loss']:.{_dec}f} "
            f"TP={levels['target']:.{_dec}f} "
            f"Units={abs(units)} "
            f"R:R=1:{levels['rr']} "
            f"EMA={state.ema:.{_dec}f} "
            "- awaiting user approval"
        )

    # ==================================================================
    # Local dashboard approval compatibility
    # ==================================================================

    def approve_proposal(
        self,
        proposal_id: str,
    ) -> Dict:
        """
        Legacy/local dashboard approval method.

        GitHub dashboard approvals are processed through
        _process_approved_proposals instead.
        """

        if self.mode != "practice":

            return {
                "ok": False,
                "error": (
                    "Manual dashboard approval "
                    "is restricted to practice mode"
                ),
            }

        if (
            self.state_reporter is None
            or not hasattr(
                self.state_reporter,
                "pop_proposal",
            )
        ):

            return {
                "ok": False,
                "error": (
                    "State reporter not available"
                ),
            }

        proposal = (
            self.state_reporter
            .pop_proposal(
                proposal_id
            )
        )

        if proposal is None:

            return {
                "ok": False,
                "error": (
                    "Proposal not found"
                ),
            }

        proposals = (
            self._load_proposals()
        )

        found = False

        for stored in proposals:

            if (
                stored.get("id")
                == proposal_id
            ):

                stored["status"] = (
                    "approved"
                )

                found = True
                break

        if not found:

            saved = dict(
                proposal
            )

            saved["status"] = (
                "approved"
            )

            proposals.append(
                saved
            )

        self._save_proposals(
            proposals
        )

        self._process_approved_proposals(
            now_london()
        )

        refreshed = (
            self._load_proposals()
        )

        for stored in refreshed:

            if (
                stored.get("id")
                == proposal_id
            ):

                if (
                    stored.get("status")
                    == "placed"
                ):

                    return {
                        "ok": True,
                        "order_id": (
                            stored.get(
                                "order_id"
                            )
                        ),
                    }

                return {
                    "ok": False,
                    "error": stored.get(
                        "error",
                        (
                            "Order was not "
                            "placed"
                        ),
                    ),
                }

        return {
            "ok": False,
            "error": (
                "Proposal result "
                "could not be found"
            ),
        }

    def reject_proposal(
        self,
        proposal_id: str,
    ) -> Dict:

        proposals = (
            self._load_proposals()
        )

        found = False

        for proposal in proposals:

            if (
                proposal.get("id")
                == proposal_id
            ):

                proposal["status"] = (
                    "rejected"
                )

                proposal[
                    "rejected_at"
                ] = (
                    now_london()
                    .strftime(
                        "%Y-%m-%d "
                        "%H:%M:%S"
                    )
                )

                found = True

                break

        if found:

            self._save_proposals(
                proposals
            )

        if (
            self.state_reporter
            is not None
            and hasattr(
                self.state_reporter,
                "pop_proposal",
            )
        ):

            self.state_reporter.pop_proposal(
                proposal_id
            )

        return {
            "ok": found,
        }

    # ==================================================================
    # Session close
    # ==================================================================

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

        # Expire pending proposals for this instrument.
        proposals = (
            self._load_proposals()
        )

        changed = False

        for proposal in proposals:

            if (
                proposal.get(
                    "instrument"
                )
                == instrument
                and proposal.get(
                    "status"
                )
                in (
                    "pending",
                    "approved",
                )
            ):

                proposal["status"] = (
                    "expired"
                )

                changed = True

        if changed:

            self._save_proposals(
                proposals
            )

    # ==================================================================
    # Dashboard snapshot
    # ==================================================================

    def _update_control_snapshot(
        self,
    ):

        control = self._load_control()

        control[
            "last_tick"
        ] = (
            now_london().strftime(
                "%H:%M:%S"
            )
        )

        control[
            "max_trades"
        ] = (
            self.config
            .max_trades_per_day
        )

        control[
            "risk_percent"
        ] = (
            self.config
            .risk_percent
        )

        try:

            control[
                "account_balance"
            ] = (
                self.client
                .get_account_balance()
            )

        except Exception as exc:

            logger.warning(
                f"Balance fetch failed: "
                f"{exc}"
            )

        try:

            control[
                "pnl_days"
            ] = (
                self.client
                .get_pnl_by_day(
                    days=3
                )
            )

        except Exception as exc:

            logger.warning(
                f"P&L fetch failed: "
                f"{exc}"
            )

        try:

            closed = (
                self.client
                .get_closed_trades()
            )

            update_trade_outcomes(
                closed
            )

        except Exception as exc:

            logger.warning(
                f"Trade outcome update "
                f"failed: {exc}"
            )

        self._save_control(
            control
        )

    # ==================================================================
    # Legacy in-process dashboard state
    # ==================================================================

    def _push_state(self):

        if self.state_reporter is None:
            return

        try:

            instruments_state = {}
            all_orders = []

            for instrument in self.instruments:

                state = (
                    self._states.get(
                        instrument
                    )
                )

                if state is None:

                    instruments_state[
                        instrument
                    ] = {
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

                    instruments_state[
                        instrument
                    ] = {
                        "ib_high": (
                            state.ib_high
                        ),
                        "ib_low": (
                            state.ib_low
                        ),
                        "ib_ready": (
                            state.ib_ready
                        ),
                        "daily_atr": (
                            state.daily_atr
                        ),
                        "ema": (
                            state.ema
                        ),
                        "trades_today": (
                            state.trades_today
                        ),
                        "long_placed": (
                            state.long_placed
                        ),
                        "short_placed": (
                            state.short_placed
                        ),
                        "long_proposed": (
                            state.long_proposed
                        ),
                        "short_proposed": (
                            state.short_proposed
                        ),
                        "session_closed": (
                            state.session_closed
                        ),
                    }

                    all_orders.extend(
                        state.last_orders
                    )

            control = (
                self._load_control()
            )

            proposals = [
                proposal
                for proposal
                in self._load_proposals()
                if proposal.get(
                    "status"
                ) == "pending"
            ]

            if self.mode == "live":
                oanda_env = "live"

            elif self.mode == "practice":
                oanda_env = "practice"

            else:
                oanda_env = "paper"

            self.state_reporter.update(
                {
                    "mode": self.mode,
                    "oanda_env": (
                        oanda_env
                    ),
                    "last_tick": (
                        control.get(
                            "last_tick"
                        )
                    ),
                    "account_balance": (
                        control.get(
                            "account_balance"
                        )
                    ),
                    "max_trades": (
                        self.config
                        .max_trades_per_day
                    ),
                    "risk_percent": (
                        self.config
                        .risk_percent
                    ),
                    "trading_enabled": (
                        control.get(
                            "trading_enabled",
                            False,
                        )
                    ),
                    "daily_budget": (
                        control.get(
                            "daily_budget"
                        )
                    ),
                    "instruments": (
                        instruments_state
                    ),
                    "orders": (
                        all_orders
                    ),
                    "pnl_days": (
                        control.get(
                            "pnl_days",
                            [],
                        )
                    ),
                    "pending_proposals": (
                        proposals
                    ),
                }
            )

        except Exception as exc:

            logger.warning(
                f"State push failed: "
                f"{exc}"
            )