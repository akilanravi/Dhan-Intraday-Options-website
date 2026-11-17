"""Opening-body cross events from completed Dhan 5-minute candles."""
from math import isfinite

import pandas as pd

IST = 'Asia/Kolkata'
FIVE = pd.Timedelta(minutes=5)
OHLC = ('Open', 'High', 'Low', 'Close')


def scan_opening_breakouts(frame, check_at_close):
    """Fix 09:15 open and 09:30 close, then detect subsequent 5m close crosses.

    Frame is sorted and capped at the scanner's published completed bar.
    The checklist callback sees only history ending at each cross.
    """
    result = {'session': None, 'opening_ready': False, 'last_5m_close': None, 'events': []}
    if frame.empty:
        return result
    if frame.index.tz is None:
        raise ValueError('Breakout candles must carry a timezone')
    last = frame.index[-1].tz_convert(IST)
    day = last.normalize()
    result['session'] = day.date().isoformat()
    opening = day + pd.Timedelta(hours=9, minutes=15)
    ready = min(last, day + pd.Timedelta(hours=15, minutes=25))
    starts = pd.date_range(opening, ready, freq=FIVE)
    pos = frame.index.get_indexer(starts)
    if len(pos) < 3 or (pos[:3] < 0).any():
        return result
    data = frame.loc[:, [*OHLC,'Volume']].to_numpy(dtype=float)
    if not all(isfinite(v) and v > 0 for v in data[pos[:3], :4].ravel()):
        return result
    ref_open, ref_close = float(data[pos[0], 0]), float(data[pos[2], 3])
    upper, lower = max(ref_open,ref_close), min(ref_open,ref_close)
    result['opening_ready'] = True
    for i in range(3,len(pos)):
        if pos[i-1] < 0 or pos[i] < 0:
            continue
        previous = float(data[pos[i-1],3])
        o,h,l,c,v = (float(x) for x in data[pos[i]])
        if not all(isfinite(x) and x > 0 for x in (previous,o,h,l,c)):
            continue
        start = starts[i]
        end = start + FIVE
        result['last_5m_close'] = end.isoformat()
        if previous <= upper < c:
            side, level = 'CE', upper
        elif previous >= lower > c:
            side, level = 'PE', lower
        else:
            continue
        result['events'].append({
            'open':o, 'high':h, 'low':l, 'close':c, 'volume':v if isfinite(v) and v >= 0 else None,
            **check_at_close(frame.iloc[:pos[i]+1], side),
            'side':side, 'level':level, 'upper':upper, 'lower':lower,
            'reference_open':ref_open, 'reference_close':ref_close, 'previous_close':previous,
            'move_points':c - level, 'move_pct':100 * (c - level) / level,
            'start':start.isoformat(), 'end':end.isoformat(),
        })
    return result
