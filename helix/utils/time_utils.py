import pytz
from datetime import datetime, time

LONDON_TZ = pytz.timezone("Europe/London")
UTC_TZ = pytz.utc


def now_london() -> datetime:
    return datetime.now(LONDON_TZ)


def to_london(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = UTC_TZ.localize(dt)
    return dt.astimezone(LONDON_TZ)


def is_ib_complete(dt_london: datetime, ib_end_hour: int = 7) -> bool:
    return dt_london.time() >= time(ib_end_hour, 0)


def is_session_active(dt_london: datetime,
                      ib_end_hour: int = 7,
                      session_end_hour: int = 18,
                      session_end_minute: int = 0) -> bool:
    t = dt_london.time()
    return time(ib_end_hour, 0) <= t < time(session_end_hour, session_end_minute)


def is_session_end(dt_london: datetime,
                   session_end_hour: int = 18,
                   session_end_minute: int = 0) -> bool:
    return dt_london.time() >= time(session_end_hour, session_end_minute)


def session_end_utc_str(dt_london: datetime,
                        session_end_hour: int = 18,
                        session_end_minute: int = 0) -> str:
    """Return today's session end (London) as RFC3339 UTC string for OANDA GTD orders."""
    end_london = LONDON_TZ.localize(
        datetime(dt_london.year, dt_london.month, dt_london.day,
                 session_end_hour, session_end_minute, 0)
    )
    end_utc = end_london.astimezone(UTC_TZ)
    return end_utc.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
