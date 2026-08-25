"""Filesystem paths — respect HELIX_DATA_DIR so persistent files (logs,
trades.csv, backtest cache and results) can be redirected to a Fly.io
volume mount (or any writable location) in production.

Locally, defaults to the project root so existing behaviour is preserved.
"""
import os

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def data_dir() -> str:
    """Root directory for all runtime state (logs, trades, caches)."""
    return os.environ.get("HELIX_DATA_DIR", _PROJECT_ROOT)


def data_path(*parts: str) -> str:
    """Path under the data directory, creating parent dirs on demand."""
    path = os.path.join(data_dir(), *parts)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return os.path.normpath(path)


def logs_dir() -> str:
    d = os.path.join(data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def backtest_cache_dir() -> str:
    # Vercel's /var/task is read-only; fall back to /tmp when the project root
    # isn't writable (works for any read-only deployment, not just Vercel).
    if os.environ.get("HELIX_DATA_DIR"):
        base = os.environ["HELIX_DATA_DIR"]
    elif os.access(_PROJECT_ROOT, os.W_OK):
        base = _PROJECT_ROOT
    else:
        base = "/tmp"
    d = os.path.join(base, "backtest_cache")
    os.makedirs(d, exist_ok=True)
    return d
