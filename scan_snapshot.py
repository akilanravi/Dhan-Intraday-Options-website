"""Compact, versioned worker snapshot shared by every live tab."""
import base64
import json
import math
import zlib

import pandas as pd
from swing_core import IST

COLUMNS=('Open','High','Low','Close','Volume','ema9','ema21','vwap',
         'st_entry','st_exit','orb_hi','orb_lo','pdh','pdl','tc','bc')
PREFIX='intraday_scan_'
STATUS='intraday_worker_status.json'
CHUNK=800_000  # Below GitHub’s inline per-file truncation threshold

def encode(rows, candles, slot, compact=False):
    def scalar(x):
        return None if not math.isfinite(float(x)) else round(float(x),6)
    frames={}
    # The live table uses inline OHLC. Keep charts for signals and a small base
    # set in the first worker publish. Full charts are attached after publishing.
    keep={r['symbol'] for r in rows if r['eligible']} | set(list(candles)[:8])
    for sym,d in candles.items():
        if compact and sym not in keep:
            continue
        if compact:
            d=d.tail(24)
        d=d.reindex(columns=COLUMNS)
        frames[sym]=[[int(t.timestamp()),*[scalar(v) for v in vals]] for t,*vals in d.itertuples()]
    payload={'version':1,'slot':str(slot) if slot is not None else '',
             'rows':rows,'frames':frames}
    compressed=zlib.compress(json.dumps(payload,separators=(',',':'),allow_nan=False).encode(),level=4)
    b64=base64.b64encode(compressed).decode('ascii')
    return [b64[i:i+CHUNK] for i in range(0,len(b64),CHUNK)]

def decode(parts):
    payload=json.loads(zlib.decompress(base64.b64decode(''.join(parts),validate=True)))
    if payload['version']!=1:
        raise ValueError('Unsupported worker snapshot version')
    frames={}
    for sym, values in payload['frames'].items():
        frame=pd.DataFrame(values,columns=('time',*COLUMNS))
        frame.index=pd.to_datetime(frame.pop('time'),unit='s',utc=True).dt.tz_convert(IST)
        frames[sym]=frame.apply(pd.to_numeric,errors='coerce')
    return payload['rows'],frames,payload['slot']
