import threading
from typing import Optional

_lock = threading.Lock()
_state = {
    "mode": "paper",
    "oanda_env": "practice",
    "trading_enabled": False,
    "daily_budget": None,
    "last_tick": None,
    "account_balance": None,
    "max_trades": 3,
    "instruments": {},
    "orders": [],
    "pnl_days": [],
    "pending_proposals": [],
}


def update(new_data: dict):
    with _lock:
        _state.update(new_data)


def get() -> dict:
    with _lock:
        return dict(_state)


# ── Proposal queue helpers ─────────────────────────────────────────────
def add_proposal(proposal: dict):
    with _lock:
        _state["pending_proposals"] = [
            p for p in _state["pending_proposals"] if p["id"] != proposal["id"]
        ]
        _state["pending_proposals"].append(proposal)


def pop_proposal(proposal_id: str) -> Optional[dict]:
    """Atomically remove and return a proposal by id, or None if not found."""
    with _lock:
        remaining = []
        found = None
        for p in _state["pending_proposals"]:
            if p["id"] == proposal_id and found is None:
                found = p
            else:
                remaining.append(p)
        _state["pending_proposals"] = remaining
        return found


def list_proposals() -> list:
    with _lock:
        return list(_state["pending_proposals"])
