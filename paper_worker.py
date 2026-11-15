"""Intraday worker. Run on an always-on host, separate from Streamlit."""
import argparse
import logging
import os
import time
from datetime import datetime
import pandas as pd
from worker_store import cloud_journal, market_open, load_env, enabled
from swing_core import IST, load_universe
from paper_engine import refresh_cycle

class NoSave:
    gist=''
    def save(self,frame):
        return 'Prepared journal update'

def cycle(env, journal, universe, now=None):
    now=now or datetime.now(IST)
    frame,_=journal.read()
    before=frame.copy(deep=True)
    if not market_open(now,env):
        return None,{'checked_at':now.isoformat(),'state':'market closed','notes':[]}
    if not env.get('DHAN_ACCESS_TOKEN') or not env.get('DHAN_CLIENT_ID'):
        raise ValueError('Dhan token and client ID required')
    stocks,indices=universe()
    lookup={x['symbol']:x for x in pd.concat([stocks,indices]).to_dict('records')}
    requested=[s.strip().upper() for s in env.get('INTRADAY_SYMBOLS','NIFTY,BANKNIFTY,RELIANCE,HDFCBANK').split(',') if s.strip()]
    selected=list(dict.fromkeys(s for s in requested if s in lookup))
    cfg=lambda name,default:float(env.get('INTRADAY_'+name,default))
    rows,_,frame,notes,_,_,_=refresh_cycle(env['DHAN_ACCESS_TOKEN'],env['DHAN_CLIENT_ID'],
        lookup,selected,frame,int(cfg('SCORE',7)),cfg('CAPITAL',100000),cfg('TRADE_CAP',35000),
        cfg('STOP_PCT',20),cfg('TARGET_R',2),cfg('RISK_PCT',1),
        enabled(env.get('ALLOW_NEW_ENTRIES','true')),NoSave(),False)
    notes=[f'Checked {len(selected)} configured symbols, {len(rows)//2} returned signals.']+ [x for x in notes if x!='Prepared journal update']
    if not selected:notes.append('No configured symbols matched Dhan.')
    failures=any('rejected authentication' in n for n in notes)
    state='Dhan access blocked' if failures else ('updated' if rows else 'no scan data')
    return (frame if not frame.equals(before) else None),{'checked_at':datetime.now(IST).isoformat(),'state':state,'notes':notes[-12:]}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    last_closed=0
    errors_last=0
    j=None
    source=[0,None]
    def universe():
        if time.monotonic()-source[0]>86400 or source[1] is None:
            source[:]=[time.monotonic(),load_universe.__wrapped__()]
        return source[1]
    while True:
        started=time.monotonic()
        try:
            j=None
            env=load_env(args.env)
            os.environ["DHAN_SHARED_LIMIT_DIR"]=env.get("DHAN_SHARED_LIMIT_DIR","")
            now=datetime.now(IST)
            if args.once or market_open(now,env) or started-last_closed>=900:
                j=cloud_journal(env.get('GITHUB_TOKEN'),env.get('INTRADAY_GIST_ID'),'intraday')
                frame,status=cycle(env,j,universe,now)
                j.write(frame,status)
                logging.info('%s: %s',status['state'],status['notes'])
                if not market_open(now,env):last_closed=started
        except Exception as e:
            logging.error('%s',str(e)[:250])
            if j is not None and time.monotonic()-errors_last>=60:
                try:
                    j.write(None,{'checked_at':datetime.now(IST).isoformat(),'state':'worker error','notes':[str(e)[:200]]})
                    errors_last=time.monotonic()
                except Exception:
                    pass
            if args.once:raise
        if args.once:return
        time.sleep(max(3,60-(time.monotonic()-started)))

if __name__=='__main__':
    main()
