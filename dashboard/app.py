import base64
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, Response, jsonify, render_template, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helix.backtest import runner as backtest_runner


app = Flask(__name__)


# ---------------------------------------------------------------------
# GitHub configuration
# ---------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get(
    "GITHUB_REPO",
    "simply-kp1/helix-fx",
)
GITHUB_BRANCH = os.environ.get(
    "GITHUB_BRANCH",
    "main",
)
GITHUB_WORKFLOW_FILE = os.environ.get(
    "GITHUB_WORKFLOW_FILE",
    "helix.yml",
)

GITHUB_API = "https://api.github.com"

HELIX_STATE_FILE = "state/helix_state.json"
PROPOSALS_FILE = "state/proposals.json"
CONTROL_FILE = "state/control.json"


# ---------------------------------------------------------------------
# Dashboard authentication
# ---------------------------------------------------------------------

_AUTH_USER = os.environ.get(
    "DASHBOARD_USERNAME",
    "helix",
)
_AUTH_PASS = os.environ.get(
    "DASHBOARD_PASSWORD",
    "",
)


def _auth_required() -> Response:
    return Response(
        "Authentication required.\n",
        401,
        {
            "WWW-Authenticate": 'Basic realm="Helix Dashboard"'
        },
    )


_AUTH_EXEMPT_PATHS = {"/healthz"}


@app.before_request
def _check_basic_auth():
    if request.path in _AUTH_EXEMPT_PATHS:
        return None

    if not _AUTH_PASS:
        return None

    auth = request.authorization

    if (
        not auth
        or auth.username != _AUTH_USER
        or auth.password != _AUTH_PASS
    ):
        return _auth_required()

    return None


# ---------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------

def _github_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = (
            f"Bearer {GITHUB_TOKEN}"
        )

    return headers


def _github_contents_url(path: str) -> str:
    return (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_REPO}/contents/{path}"
    )


def _read_json(
    path: str,
    default: Any,
) -> Any:
    """
    Read a JSON file from GitHub.

    Returns default if the file does not exist or
    cannot be decoded.
    """

    try:
        response = requests.get(
            _github_contents_url(path),
            headers=_github_headers(),
            params={
                "ref": GITHUB_BRANCH,
                "_": int(time.time()),
            },
            timeout=15,
        )

        if response.status_code == 404:
            return default

        response.raise_for_status()

        payload = response.json()

        encoded = payload.get("content", "")
        encoded = encoded.replace("\n", "")

        raw = base64.b64decode(
            encoded
        ).decode("utf-8")

        return json.loads(raw)

    except Exception as exc:
        app.logger.warning(
            "Could not read %s from GitHub: %s",
            path,
            exc,
        )

        return default


def _get_file_sha(
    path: str,
) -> Optional[str]:
    try:
        response = requests.get(
            _github_contents_url(path),
            headers=_github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=15,
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()

        return response.json().get("sha")

    except Exception as exc:
        app.logger.warning(
            "Could not get SHA for %s: %s",
            path,
            exc,
        )

        return None


def _write_json(
    path: str,
    data: Any,
    commit_message: str,
) -> bool:
    """
    Create or update a JSON file in GitHub.

    Retries if another GitHub process modifies the file
    at roughly the same time.
    """

    if not GITHUB_TOKEN:
        app.logger.error(
            "GITHUB_TOKEN is not configured"
        )
        return False

    encoded_content = base64.b64encode(
        json.dumps(
            data,
            indent=2,
        ).encode("utf-8")
    ).decode("ascii")

    for attempt in range(3):
        try:
            sha = _get_file_sha(path)

            body = {
                "message": commit_message,
                "content": encoded_content,
                "branch": GITHUB_BRANCH,
            }

            if sha:
                body["sha"] = sha

            response = requests.put(
                _github_contents_url(path),
                headers=_github_headers(),
                json=body,
                timeout=20,
            )

            if response.status_code in (
                200,
                201,
            ):
                return True

            # A conflicting commit may have landed between
            # reading the SHA and writing the file.
            if response.status_code in (
                409,
                422,
            ):
                time.sleep(1)
                continue

            app.logger.error(
                "GitHub write failed for %s: %s %s",
                path,
                response.status_code,
                response.text,
            )

            return False

        except Exception as exc:
            app.logger.warning(
                "GitHub write attempt %s failed "
                "for %s: %s",
                attempt + 1,
                path,
                exc,
            )

            time.sleep(1)

    return False


def _trigger_helix() -> bool:
    """
    Trigger the Helix GitHub Actions workflow immediately.
    """

    if not GITHUB_TOKEN:
        return False

    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_REPO}/actions/workflows/"
        f"{GITHUB_WORKFLOW_FILE}/dispatches"
    )

    try:
        response = requests.post(
            url,
            headers=_github_headers(),
            json={
                "ref": GITHUB_BRANCH,
            },
            timeout=15,
        )

        if response.status_code == 204:
            return True

        app.logger.error(
            "Could not trigger Helix workflow: "
            "%s %s",
            response.status_code,
            response.text,
        )

        return False

    except Exception as exc:
        app.logger.warning(
            "Could not trigger Helix workflow: %s",
            exc,
        )

        return False


# ---------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------

def _default_control() -> Dict:
    return {
        "trading_enabled": False,
        "daily_budget": None,
    }


def _load_control() -> Dict:
    stored = _read_json(
        CONTROL_FILE,
        _default_control(),
    )

    control = _default_control()

    if isinstance(stored, dict):
        control.update(stored)

    return control


def _load_proposals() -> List[Dict]:
    proposals = _read_json(
        PROPOSALS_FILE,
        [],
    )

    if not isinstance(
        proposals,
        list,
    ):
        return []

    return proposals


def _load_helix_state() -> Dict:
    state = _read_json(
        HELIX_STATE_FILE,
        {},
    )

    if not isinstance(
        state,
        dict,
    ):
        return {}

    return state


def _build_dashboard_state() -> Dict:
    """
    Convert the GitHub files into the structure already
    expected by index.html.
    """

    helix_state = _load_helix_state()
    proposals = _load_proposals()
    control = _load_control()

    pending_proposals = [
        proposal
        for proposal in proposals
        if proposal.get(
            "status",
            "pending",
        ) == "pending"
    ]

    orders = []

    instruments = {}

    for instrument, state in helix_state.items():
        if not isinstance(
            state,
            dict,
        ):
            continue

        instruments[instrument] = {
            "ib_high": state.get(
                "ib_high"
            ),
            "ib_low": state.get(
                "ib_low"
            ),
            "ib_ready": state.get(
                "ib_ready",
                False,
            ),
            "daily_atr": state.get(
                "daily_atr"
            ),
            "ema": state.get(
                "ema"
            ),
            "trades_today": state.get(
                "trades_today",
                0,
            ),
            "long_placed": state.get(
                "long_placed",
                False,
            ),
            "short_placed": state.get(
                "short_placed",
                False,
            ),
            "long_proposed": state.get(
                "long_proposed",
                False,
            ),
            "short_proposed": state.get(
                "short_proposed",
                False,
            ),
            "session_closed": state.get(
                "session_closed",
                False,
            ),
        }

        for order in state.get(
            "last_orders",
            [],
        ):
            orders.append(order)

    return {
        "mode": "practice",
        "oanda_env": "practice",
        "trading_enabled": control.get(
            "trading_enabled",
            False,
        ),
        "daily_budget": control.get(
            "daily_budget"
        ),
        "last_tick": control.get(
            "last_tick"
        ),
        "account_balance": control.get(
            "account_balance"
        ),
        "max_trades": control.get(
            "max_trades",
            3,
        ),
        "risk_percent": control.get(
            "risk_percent",
            1.0,
        ),
        "instruments": instruments,
        "orders": orders,
        "pnl_days": control.get(
            "pnl_days",
            [],
        ),
        "pending_proposals": (
            pending_proposals
        ),
    }


def _change_proposal_status(
    proposal_id: str,
    new_status: str,
) -> bool:
    proposals = _load_proposals()

    found = False

    for proposal in proposals:
        if proposal.get("id") == proposal_id:

            # Only pending proposals may be changed
            # through the dashboard.
            if proposal.get(
                "status",
                "pending",
            ) != "pending":
                return False

            proposal["status"] = (
                new_status
            )

            found = True
            break

    if not found:
        return False

    return _write_json(
        PROPOSALS_FILE,
        proposals,
        (
            f"Helix proposal "
            f"{proposal_id}: {new_status}"
        ),
    )


# ---------------------------------------------------------------------
# Main dashboard routes
# ---------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return {
        "ok": True,
    }, 200


@app.route("/")
def index():
    return render_template(
        "index.html"
    )


@app.route("/api/state")
def api_state():
    return jsonify(
        _build_dashboard_state()
    )


# ---------------------------------------------------------------------
# Start / stop trading
# ---------------------------------------------------------------------

@app.route(
    "/api/start",
    methods=["POST"],
)
def api_start():
    body = (
        request.get_json(
            silent=True
        )
        or {}
    )

    try:
        budget = float(
            body.get(
                "budget",
                0,
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        budget = 0

    if budget <= 0:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Budget must be "
                    "greater than 0"
                ),
            }
        ), 400

    control = _load_control()

    control["trading_enabled"] = True
    control["daily_budget"] = budget

    ok = _write_json(
        CONTROL_FILE,
        control,
        "Start Helix trading",
    )

    if not ok:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Could not update "
                    "GitHub control state"
                ),
            }
        ), 500

    return jsonify(
        {
            "ok": True,
            "budget": budget,
        }
    )


@app.route(
    "/api/stop",
    methods=["POST"],
)
def api_stop():
    control = _load_control()

    control["trading_enabled"] = False

    ok = _write_json(
        CONTROL_FILE,
        control,
        "Stop Helix trading",
    )

    if not ok:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Could not update "
                    "GitHub control state"
                ),
            }
        ), 500

    return jsonify(
        {
            "ok": True,
        }
    )


# ---------------------------------------------------------------------
# Proposal approval / rejection
# ---------------------------------------------------------------------

@app.route(
    "/api/approve",
    methods=["POST"],
)
def api_approve():
    body = (
        request.get_json(
            silent=True
        )
        or {}
    )

    proposal_id = body.get("id")

    if not proposal_id:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Missing proposal id"
                ),
            }
        ), 400

    ok = _change_proposal_status(
        proposal_id,
        "approved",
    )

    if not ok:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Proposal not found, "
                    "already handled, or "
                    "GitHub update failed"
                ),
            }
        ), 400

    # Wake Helix immediately so it can process
    # the approved proposal.
    triggered = _trigger_helix()

    return jsonify(
        {
            "ok": True,
            "status": "approved",
            "workflow_triggered": triggered,
        }
    )


@app.route(
    "/api/reject",
    methods=["POST"],
)
def api_reject():
    body = (
        request.get_json(
            silent=True
        )
        or {}
    )

    proposal_id = body.get("id")

    if not proposal_id:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Missing proposal id"
                ),
            }
        ), 400

    ok = _change_proposal_status(
        proposal_id,
        "rejected",
    )

    if not ok:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Proposal not found, "
                    "already handled, or "
                    "GitHub update failed"
                ),
            }
        ), 400

    return jsonify(
        {
            "ok": True,
            "status": "rejected",
        }
    )


# ---------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------

@app.route("/backtest")
def backtest_page():
    return render_template(
        "backtest.html"
    )


@app.route(
    "/api/backtest/run",
    methods=["POST"],
)
def api_backtest_run():
    body = (
        request.get_json(
            silent=True
        )
        or {}
    )

    instruments = (
        body.get("instruments")
        or [
            "EUR_USD",
            "GBP_USD",
            "USD_JPY",
        ]
    )

    years = int(
        body.get(
            "years",
            5,
        )
    )

    starting_balance = float(
        body.get(
            "starting_balance",
            10000,
        )
    )

    risk_pct = float(
        body.get(
            "risk_pct",
            1.0,
        )
    )

    frictions = bool(
        body.get(
            "frictions",
            True,
        )
    )

    result = (
        backtest_runner.start_backtest(
            instruments=instruments,
            years=years,
            starting_balance=starting_balance,
            risk_pct=risk_pct,
            frictions=frictions,
        )
    )

    return jsonify(
        result
    ), (
        200
        if result.get("ok")
        else 400
    )


@app.route(
    "/api/backtest/status"
)
def api_backtest_status():
    return jsonify(
        backtest_runner.get_state()
    )


@app.route(
    "/api/backtest/results"
)
def api_backtest_results():
    results = (
        backtest_runner.load_last_results()
    )

    if results is None:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "No results yet"
                ),
            }
        ), 404

    return jsonify(results)


def create_app(agent=None):
    return app