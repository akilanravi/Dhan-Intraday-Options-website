"""15-minute opening-range events built from the scanner's completed 5-minute data."""
from math import isfinite

import pandas as pd

IST = 'Asia/Kolkata'
FIVE = pd.Timedelta(minutes=5)
FIFTEEN = 3 * FIVE
OHLC = ('Open', 'High', 'Low', 'Close')


def scan_opening_breakouts(frame, check_at_close):
    """Return one session's events and 15-minute data coverage.

    Input is enriched, sorted, unique Dhan data capped at the scanner's
    published bar. check_at_close gets only the prefix ending at the event.
    """
    result = {'session': None, 'opening_ready': False, 'last_15m_close': None, 'events': []}
    if frame.empty:
        return result
    if frame.index.tz is None:
        raise ValueError('Breakout candles must carry a timezone')
    latest = frame.index[-1].tz_convert(IST)
    session = latest.normalize()
    result['session'] = session.date().isoformat()
    opening = session + pd.Timedelta(hours=9, minutes=15)
    end = session + pd.Timedelta(hours=15, minutes=30)
    ready = min(latest + FIVE, end)
    if ready < opening + FIFTEEN:
        return result

    starts = pd.date_range(opening, ready - FIFTEEN, freq=FIFTEEN)
    wanted = pd.date_range(opening, periods=3*len(starts), freq=FIVE)
    positions = frame.index.get_indexer(wanted).reshape(-1, 3)
    valid = (positions >= 0).all(axis=1)
    columns = frame.loc[:, [*OHLC, 'Volume']].to_numpy(dtype=float)
    bins = {}
    for start, group in zip(starts[valid], positions[valid]):
        bars = columns[group]
        if not all(isfinite(x) and x > 0 for x in bars[:, :4].ravel()):
            continue
        vol = bars[:, 4]
        bins[start] = ({
            'open': float(bars[0, 0]), 'high': float(max(bars[:, 1])),
            'low': float(min(bars[:, 2])), 'close': float(bars[-1, 3]),
            'volume': float(sum(vol)) if all(isfinite(v) and v >= 0 for v in vol) else None,
        }, int(group[-1]))

    if opening not in bins:
        return result
    result['opening_ready'] = True
    result['last_15m_close'] = (max(bins) + FIFTEEN).isoformat()
    opening_bar, _ = bins[opening]
    hi, lo = opening_bar['high'], opening_bar['low']
    for start, (bar, final_pos) in bins.items():
        if start == opening:
            continue
        o, c = bar['open'], bar['close']
        if o <= hi < c:
            side, level = 'CE', hi
        elif o >= lo > c:
            side, level = 'PE', lo
        else:
            continue
        check = check_at_close(frame.iloc[:final_pos+1], side)
        result['events'].append({
            **bar, **check, 'side': side, 'level': level,
            'opening_high': hi, 'opening_low': lo, 'session_open': opening_bar['open'],
            'move_points': c - level, 'move_pct': 100 * (c - level) / level,
            'start': start.isoformat(), 'end': (start + FIFTEEN).isoformat(),
        })
    return result
