"""One durable journal writer per strategy. Dashboards only read in worker mode."""
import json
import uuid
import re
import os
import requests
from paper_engine import read_csv
from scan_snapshot import decode, PREFIX
from scan_clock import regular_day, write_window

def enabled(value):
    return str(value).strip().lower() in ('1','true','yes','on')

class CloudJournal:
    def __init__(self, token, gist, strategy):
        if not token or not re.fullmatch(r'[a-fA-F0-9]{16,64}', str(gist or '')):
            raise ValueError('A GitHub token and an existing Gist ID are required')
        self.url=f'https://api.github.com/gists/{gist}'
        self.headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'}
        self.csv={'intraday':'intraday_paper_trades.csv','swing':'paper_trades.csv'}[strategy]
        self.state=f'{strategy}_worker_status.json'
        self.etag=''

    def read(self, snapshot=False, previous_id=''):
        headers={**self.headers,**({'If-None-Match':self.etag} if snapshot and self.etag and previous_id else {})}
        r=requests.get(self.url,headers=headers,timeout=12)
        if r.status_code==304 and snapshot:return None,None,None
        if not r.ok:raise RuntimeError(f'GitHub journal read HTTP {r.status_code}')
        files=r.json()['files']
        def content(name):
            f=files.get(name)
            if not f:return ''
            if f.get('truncated'):
                raw=requests.get(f['raw_url'],headers=self.headers,timeout=12)
                if not raw.ok:raise RuntimeError(f'GitHub raw file read HTTP {raw.status_code}')
                return raw.text
            return f.get('content','')
        frame=read_csv(content(self.csv))
        raw=content(self.state)
        status=json.loads(raw) if raw else {}
        if not snapshot:return frame,status
        n=status.get('snapshot_parts',0)
        snap=decode([content(f'{PREFIX}{i:02}.txt') for i in range(n)]) if n and status.get('snapshot_id')!=previous_id else None
        self.etag=r.headers.get('ETag','')
        return frame,status,snap

    def write(self, frame, state, chunks=None, previous=None):
        previous=previous or {}
        state=dict(state)
        if chunks is not None:
            state['snapshot_parts']=len(chunks)
            state['snapshot_id']=uuid.uuid4().hex
        else:
            for key in ('snapshot_id','snapshot_parts'):
                if key in previous:state[key]=previous[key]
        files={self.state:{'content':json.dumps(state,ensure_ascii=False)}}
        if frame is not None and len(frame.columns):
            files[self.csv]={'content':frame.to_csv(index=False)}
        if chunks is not None:
            for i,chunk in enumerate(chunks):
                files[f'{PREFIX}{i:02}.txt']={'content':chunk}
            for i in range(len(chunks),previous.get('snapshot_parts',0)):
                files[f'{PREFIX}{i:02}.txt']=None
        r=requests.patch(self.url,headers=self.headers,json={'files':files},timeout=25)
        if not r.ok:raise RuntimeError(f'GitHub journal write HTTP {r.status_code}')
        return state

def cloud_journal(token,gist,strategy):
    return CloudJournal(token,gist,strategy)

def market_open(now, env):
    return regular_day(now,env) and write_window(now)

def load_env(path):
    """Read simple KEY=value lines each cycle so token changes take effect."""
    from pathlib import Path
    pairs={}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            key,val=line.split('=',1)
            pairs[key.strip()]=val.strip()
    return {**os.environ,**pairs}
