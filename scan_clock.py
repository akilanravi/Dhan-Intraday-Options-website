"""Regular NSE session clock. Candle IDs denote bar starts in India time."""
import pandas as pd
from swing_core import IST

BAR = pd.Timedelta(minutes=5)
FEED_GRACE = pd.Timedelta(seconds=2)

def timestamp(value):
    t = pd.Timestamp(value)
    return t.tz_localize(IST) if t.tz is None else t.tz_convert(IST)

def completed_slot(clock):
    return timestamp(clock).floor('5min') - BAR

def fresh_candle(candle, clock):
    if candle is None or not candle:
        return False
    try:
        t, now = timestamp(candle), timestamp(clock)
        return t.date() == now.date() and t == completed_slot(now) and (9,15) <= (t.hour,t.minute) < (15,30)
    except (ValueError, TypeError, AttributeError):
        return False

def regular_day(clock, settings):
    now = timestamp(clock)
    holidays = {v.strip() for v in str(settings.get('NSE_CLOSED_DATES','')).split(',')}
    return now.weekday() < 5 and now.date().isoformat() not in holidays

def scheduled_slot(clock, settings):
    """Includes the last bar (15:25–15:30), which is scan-only."""
    now = timestamp(clock)
    if not regular_day(now, settings) or not ((9,20) <= (now.hour,now.minute) < (15,35)):
        return None
    slot = completed_slot(now)
    return slot if now >= slot + BAR + FEED_GRACE else None

def write_window(clock):
    now = timestamp(clock)
    return now.weekday() < 5 and (9,15) <= (now.hour,now.minute) < (15,30)

def entry_window(clock):
    now = timestamp(clock)
    return now.weekday() < 5 and (9,45) <= (now.hour,now.minute) < (15,10)
