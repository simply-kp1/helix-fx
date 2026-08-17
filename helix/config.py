import os
import re
from pathlib import Path
import yaml
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

# Locate .env relative to this file so it works regardless of cwd
load_dotenv(Path(__file__).parent.parent / ".env", override=True)


def _expand_env(value: str) -> str:
    """Replace ${VAR} placeholders with environment variable values."""
    if not isinstance(value, str):
        return value
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value)


@dataclass
class BrokerConfig:
    name: str
    account_id: str
    api_key: str
    environment: str


@dataclass
class Config:
    broker: BrokerConfig
    instruments: List[str]
    risk_percent: float
    max_trades_per_day: int
    ema_period: int
    atr_period: int
    atr_buffer_pct: float
    min_rr: float
    ib_start_hour: int
    ib_end_hour: int
    session_end_hour: int
    timezone: str

    @classmethod
    def from_file(cls, path: str = "config.yaml") -> "Config":
        with open(path, "r") as f:
            raw = yaml.safe_load(f)

        b = raw["broker"]
        t = raw["trading"]
        s = raw["strategy"]
        sess = raw["session"]

        return cls(
            broker=BrokerConfig(
                name=b["name"],
                account_id=_expand_env(b["account_id"]),
                api_key=_expand_env(b["api_key"]),
                environment=_expand_env(b.get("environment", "practice")),
            ),
            instruments=t["instruments"],
            risk_percent=float(t.get("risk_percent", 1.0)),
            max_trades_per_day=int(t.get("max_trades_per_day", 3)),
            ema_period=int(s.get("ema_period", 100)),
            atr_period=int(s.get("atr_period", 14)),
            atr_buffer_pct=float(s.get("atr_buffer_pct", 0.05)),
            min_rr=float(s.get("min_rr", 1.5)),
            ib_start_hour=int(sess.get("ib_start_hour", 6)),
            ib_end_hour=int(sess.get("ib_end_hour", 7)),
            session_end_hour=int(sess.get("session_end_hour", 18)),
            timezone=sess.get("timezone", "Europe/London"),
        )
