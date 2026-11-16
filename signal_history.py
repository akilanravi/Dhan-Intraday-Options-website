"""Deduplicated IST candle-close history, independent of paper fills."""
import json
import re
import requests
from pathlib import Path
from scan_clock import BAR, timestamp

def collect(history, rows, slot):
    if slot is None:
        return history
    bar = timestamp(slot)
    day = bar.date().isoformat()
    close = (bar + BAR).strftime('%H:%M')
    if history.get('date', '') > day:
        return history
    values = {k:list(v) for k,v in history.get('times',{}).items()} if history.get('date') == day else {}
    for row in rows:
        if row.get('eligible') and timestamp(row['candle']) == bar:
            key = f"{row['symbol']} {row['side']}"
            if close not in values.setdefault(key,[]):
                values[key].append(close)
                values[key].sort()
    return {'date':day,'times':values}

def times_for(history, row):
    if history.get('date') != timestamp(row['candle']).date().isoformat():
        return ''
    return ', '.join(history.get('times',{}).get(f"{row['symbol']} {row['side']}",[]))

def read_local():
    p=Path('intraday_signal_times.json')
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}

def save_local(history):
    path=Path('intraday_signal_times.json')
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(history,separators=(',',':')), encoding='utf-8')
    tmp.replace(path)


HISTORY_FILE='intraday_signal_times.json'

def read_gist(token,gist):
    if not re.fullmatch(r'[a-fA-F0-9]{16,64}',gist):
        raise ValueError('Invalid Gist ID')
    headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'}
    r=requests.get(f'https://api.github.com/gists/{gist}',headers=headers,timeout=10)
    r.raise_for_status()
    f=r.json()['files'].get(HISTORY_FILE)
    if not f:
        return {}
    if f.get('truncated'):
        raw=requests.get(f['raw_url'],headers=headers,timeout=10)
        raw.raise_for_status()
        return json.loads(raw.text)
    return json.loads(f['content'])

def save_gist(token,gist,history):
    if not re.fullmatch(r'[a-fA-F0-9]{16,64}',gist):
        raise ValueError('Invalid Gist ID')
    headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'}
    r=requests.patch(f'https://api.github.com/gists/{gist}',headers=headers,timeout=10,
                     json={'files':{HISTORY_FILE:{'content':json.dumps(history,separators=(',',':'))}}})
    r.raise_for_status()

def merge(a,b):
    if a.get('date') != b.get('date'):
        return max((a,b),key=lambda v:v.get('date',''))
    if not a:
        return b
    keys=set(a.get('times',{}))|set(b.get('times',{}))
    return {'date':a['date'],'times':{k:sorted(set(a.get('times',{}).get(k,()))|set(b.get('times',{}).get(k,()))) for k in keys}}
