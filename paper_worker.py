"""Unattended intraday scanner and paper writer. Run exactly one worker per Gist."""
import argparse
import logging
import os
import time
from datetime import datetime
import pandas as pd
from worker_store import cloud_journal, load_env, enabled
from swing_core import IST, load_universe
from paper_engine import refresh_cycle
from scan_clock import scheduled_slot, regular_day
from scan_snapshot import encode

class NoSave:
    gist=''
    def save(self,frame):
        return 'Prepared journal update'


def cycle(env, journal, universe, now=None, pending=None):
    now=now or datetime.now(IST)
    slot=scheduled_slot(now,env)
    if slot is None:return False
    frame,previous=journal.read()
    if previous.get('scan_slot')==str(slot):
        return True
    if not env.get('DHAN_ACCESS_TOKEN') or not env.get('DHAN_CLIENT_ID'):
        raise ValueError('Dhan token and client ID required')
    stocks,indices=universe()
    lookup={x['symbol']:x for x in pd.concat([stocks,indices]).to_dict('records')}
    selected=list(lookup)
    cfg=lambda name,default:float(env.get('INTRADAY_'+name,default))
    policy=dict(threshold=int(cfg('SCORE',7)),capital=cfg('CAPITAL',100000),cap=cfg('TRADE_CAP',35000),
                stop=cfg('STOP_PCT',20),target=cfg('TARGET_R',2),risk=cfg('RISK_PCT',1),
                auto=enabled(env.get('ALLOW_NEW_ENTRIES','true')))
    if not (5<=policy['threshold']<=8 and policy['capital']>0 and 0<policy['cap']<=35000
            and 0<policy['stop']<100 and policy['target']>0 and 0<policy['risk']<=100):
        raise ValueError('Invalid paper policy. Check settings in worker.env.')
    rows,candles,after,notes,_,_,stamp=refresh_cycle(env['DHAN_ACCESS_TOKEN'],env['DHAN_CLIENT_ID'],
        lookup,selected,frame,**policy,store=NoSave(),dirty=False,slot=slot,allow_trades=regular_day(now,env))
    notes=[x for x in notes if x!='Prepared journal update']
    failures=any('rejected authentication' in n for n in notes)
    state='Dhan access blocked' if failures else ('updated' if len(rows)==2*len(selected) else
                                                 ('partial coverage' if rows else 'no scan data'))
    status={'checked_at':stamp,'scan_slot':str(slot),'state':state,'notes':notes[:30],
            'requested':len(selected),'scanned':len(rows)//2,'policy':policy}
    payload=(after if not after.equals(frame) else None,status,
             encode(rows,candles,slot) if rows else None,previous)
    if pending is not None:pending[:] = [journal,slot,payload]
    journal.write(*payload)
    if pending is not None:pending.clear()
    logging.info('%s: %s',state,notes[:4])
    return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    last_slot=None
    pending=[]
    source=[0,None]
    def universe():
        if time.monotonic()-source[0]>14400 or source[1] is None:
            source[:]=[time.monotonic(),load_universe.__wrapped__()]
        return source[1]
    while True:
        slot=None
        previous={}
        j=None
        try:
            if pending:
                saved,saved_slot,payload=pending
                _,prior=saved.read()
                if prior.get('scan_slot')!=str(saved_slot):saved.write(*payload)
                last_slot=saved_slot
                pending.clear()
            env=load_env(args.env)
            if env.get('DHAN_SHARED_LIMIT_DIR'):os.environ['DHAN_SHARED_LIMIT_DIR']=env['DHAN_SHARED_LIMIT_DIR']
            now=datetime.now(IST)
            slot=scheduled_slot(now,env)
            if slot is not None and slot!=last_slot:
                j=cloud_journal(env.get('GITHUB_TOKEN'),env.get('INTRADAY_GIST_ID'),'intraday')
                if cycle(env,j,universe,now,pending):last_slot=slot
            elif args.once:
                logging.info('No eligible new closed candle right now.')
        except Exception as e:
            logging.error('%s',str(e)[:250])
            if j is not None:
                try:
                    _,previous=j.read()
                    if previous.get('scan_slot')==str(slot):
                        # PATCH may have succeeded before the response was lost.
                        last_slot=slot
                        pending.clear()
                    else:
                        j.write(None,{**previous,'checked_at':datetime.now(IST).isoformat(),
                                      'state':'worker error','notes':[str(e)[:200]]},previous=previous)
                except Exception:
                    pass
            if args.once:raise
            time.sleep(12)
        if args.once:return
        time.sleep(2)

if __name__=='__main__':
    main()
