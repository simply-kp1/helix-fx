import os
import sys

from flask import Flask, Response, jsonify, render_template, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import state as shared_state
from helix.backtest import runner as backtest_runner

app = Flask(__name__)
_agent = None  # set by create_app()


# ── Optional HTTP basic-auth gate ─────────────────────────────────────
# If DASHBOARD_PASSWORD is set, every route is behind the browser's
# basic-auth prompt. Username = "helix" by default (override with
# DASHBOARD_USERNAME). This is enough for a personal single-user deploy;
# for multi-user consider a proper auth layer instead.
_AUTH_USER = os.environ.get("DASHBOARD_USERNAME", "helix")
_AUTH_PASS = os.environ.get("DASHBOARD_PASSWORD", "")


def _auth_required() -> Response:
    return Response(
        "Authentication required.\n", 401,
        {"WWW-Authenticate": 'Basic realm="Helix Dashboard"'},
    )


_AUTH_EXEMPT_PATHS = {"/healthz"}


@app.before_request
def _check_basic_auth():
    if request.path in _AUTH_EXEMPT_PATHS:
        return None
    if not _AUTH_PASS:
        return None  # auth disabled → open dashboard (dev/local mode)
    auth = request.authorization
    if not auth or auth.username != _AUTH_USER or auth.password != _AUTH_PASS:
        return _auth_required()
    return None


@app.route("/healthz")
def healthz():
    return {"ok": True}, 200


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(shared_state.get())


@app.route("/api/start", methods=["POST"])
def api_start():
    body = request.get_json(silent=True) or {}
    budget = float(body.get("budget", 0))
    if budget <= 0:
        return jsonify({"ok": False, "error": "Budget must be greater than 0"}), 400
    shared_state.update({"trading_enabled": True, "daily_budget": budget})
    return jsonify({"ok": True, "budget": budget})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    shared_state.update({"trading_enabled": False})
    return jsonify({"ok": True})


@app.route("/api/approve", methods=["POST"])
def api_approve():
    if _agent is None:
        return jsonify({"ok": False, "error": "Agent not attached"}), 500
    body = request.get_json(silent=True) or {}
    proposal_id = body.get("id")
    if not proposal_id:
        return jsonify({"ok": False, "error": "Missing proposal id"}), 400
    result = _agent.approve_proposal(proposal_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/reject", methods=["POST"])
def api_reject():
    if _agent is None:
        return jsonify({"ok": False, "error": "Agent not attached"}), 500
    body = request.get_json(silent=True) or {}
    proposal_id = body.get("id")
    if not proposal_id:
        return jsonify({"ok": False, "error": "Missing proposal id"}), 400
    result = _agent.reject_proposal(proposal_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    body = request.get_json(silent=True) or {}
    instruments = body.get("instruments") or ["EUR_USD", "GBP_USD", "USD_JPY"]
    years = int(body.get("years", 5))
    starting_balance = float(body.get("starting_balance", 10000))
    risk_pct = float(body.get("risk_pct", 1.0))
    frictions = bool(body.get("frictions", True))
    result = backtest_runner.start_backtest(
        instruments=instruments,
        years=years,
        starting_balance=starting_balance,
        risk_pct=risk_pct,
        frictions=frictions,
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/backtest/status")
def api_backtest_status():
    return jsonify(backtest_runner.get_state())


@app.route("/api/backtest/results")
def api_backtest_results():
    results = backtest_runner.load_last_results()
    if results is None:
        return jsonify({"ok": False, "error": "No results yet"}), 404
    return jsonify(results)


def create_app(agent=None):
    global _agent
    _agent = agent
    return app
