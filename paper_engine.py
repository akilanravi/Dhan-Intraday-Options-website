"""Dhan Intraday Options V1. Separate repository, paper-only, automatic in-session refresh."""
import hashlib, io, math, os, time
from threading import Lock
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from queue import SimpleQueue
import pandas as pd
import requests
import streamlit as st
from intraday import intraday_candles,enrich,signal,choose_option
from swing_core import DhanAPI,load_universe,IST

class PaperStore:
    """Dedicated intraday journal; private Gist recommended for Streamlit persistence."""
    def __init__(self,token='',gist=''):
        self.token=token;self.gist=gist;self.path=Path('intraday_paper_trades.csv')
        self.headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'}
    def load(self):
        if self.token and self.gist:
            r=requests.get(f'https://api.github.com/gists/{self.gist}',headers=self.headers,timeout=20);r.raise_for_status()
            f=r.json()['files'].get('intraday_paper_trades.csv')
            if f:
                data=f.get('content') if not f.get('truncated') else requests.get(f['raw_url'],headers=self.headers,timeout=20).text
                return pd.read_csv(io.StringIO(data)).fillna('')
        return pd.read_csv(self.path).fillna('') if self.path.exists() else pd.DataFrame()
    def save(self,d):
        data=d.to_csv(index=False);tmp = self.path.with_suffix('.tmp')
        tmp.write_text(data, encoding='utf-8'); os.replace(tmp, self.path)
        if not self.token:return 'Local save only; Streamlit restart may erase trades.'
        if self.gist:
            r=requests.patch(f'https://api.github.com/gists/{self.gist}',headers=self.headers,json={'files':{'intraday_paper_trades.csv':{'content':data}}},timeout=20)
        else:
            r=requests.post('https://api.github.com/gists',headers=self.headers,json={'description':'Dhan intraday PAPER trades','public':False,'files':{'intraday_paper_trades.csv':{'content':data}}},timeout=20)
            if r.ok:self.gist=r.json()['id']
        r.raise_for_status()
        return f'Saved to Gist {self.gist}'

@st.cache_data(ttl=86400,show_spinner=False)
def universe():
    stocks,indices=load_universe()
    return pd.concat([indices,stocks],ignore_index=True).drop_duplicates('symbol')

def quote(api,x,contract):
    spot,chain=__import__('swing_core').parse_chain(api.chain(x['sid'],x['segment'],contract['expiry']))
    rows=chain[(chain.type==contract['side'])&(chain.strike==float(contract['strike']))]
    if rows.empty:raise ValueError('Option contract not present in current chain')
    row=rows.iloc[0]
    price=float(row.bid)
    if not (math.isfinite(price) and price>0):raise ValueError('No executable bid price')
    return price

def update_positions(j,api,lookup,signals,sl_pct,target_r,refresh_time):
    if j.empty:return j,[]
    messages=[]
    for i,t in j[j.status=='OPEN'].iterrows():
        symbol=str(t.symbol)
        if symbol not in lookup:messages.append(f'{symbol}: instrument unavailable');continue
        x=lookup[symbol]
        try:
            live=quote(api,x,t)
            entry=float(t.entry_premium); side=t.side
            stop=float(t.get('stop_premium') or entry*(1-sl_pct/100))
            target=float(t.get('target_premium') or entry*(1+sl_pct/100*target_r))
            current=signals.get((symbol,side),{})
            # No future-looking execution: first observed quote is a snapshot, not a stop-fill guarantee.
            opposite=signals.get((symbol,'PE' if side=='CE' else 'CE'),{})
            reversal=bool(current.get('reverse_now')) or bool(opposite.get('eligible'))
            # End of day flat; 15:20 IST cut-off
            clock=datetime.now(IST)
            if not ((9,15)<=(clock.hour,clock.minute)<(15,30)) or clock.weekday()>=5:
                messages.append(f'{symbol}: quote arrived outside market hours; left OPEN')
                continue
            eod=(clock.hour,clock.minute)>=(15,20) or str(t.opened_at)[:10] != clock.date().isoformat()
            reason='TARGET' if live>=target else 'STOP' if live<=stop else 'REVERSAL' if reversal else 'EOD' if eod else ''
            j.at[i,'last_bid']=live;j.at[i,'last_checked']=refresh_time
            if reason:
                qty=int(float(t.lot))*int(float(t.lots))
                # Estimated friction: ₹20 brokerage each side, sell-side STT and other charges approximated.
                gross=(live-entry)*qty; charges=40+0.002*(entry+live)*qty
                j.at[i,'pnl']=round(gross-charges,2)
                j.at[i,'status']='CLOSED';j.at[i,'exit_premium']=live;j.at[i,'exit_reason']=reason;j.at[i,'closed_at']=refresh_time
                messages.append(f'{symbol} {side}: closed on {reason} at bid ₹{live:.2f} (snapshot)')
        except Exception as e:messages.append(f'{symbol}: quote unavailable — {str(e)[:90]}; left OPEN')
    return j,messages

def create_entries(j,api,lookup,signals,capital,cap,sl_pct,target_r,risk_pct,auto,now):
    messages=[]
    if not auto:return j,messages
    clock=datetime.now(IST)
    if clock.weekday()>=5 or not ((9,45)<=(clock.hour,clock.minute)<(15,10)):
        return j,['Outside 09:45–15:10 IST: no new entries.']
    opened=0
    for (symbol,side),s in sorted(signals.items(),key=lambda z:-z[1]['score']):
        clock=datetime.now(IST)
        if (clock.hour,clock.minute)>=(15,10) or opened>=2:break
        if not s['eligible'] or not fresh_candle(s['candle'],clock):continue
        if not j.empty and ((j.status=='OPEN')&(j.symbol==symbol)).any():continue
        if not j.empty and ((j.symbol==symbol)&(j.side==side)&(j.signal_candle==s['candle'])).any():continue
        if not j.empty and (j.status=='OPEN').sum()>=3:break
        if not j.empty and ((j.opened_at.astype(str).str[:10]==clock.date().isoformat())).sum()>=2:break
        x=lookup[symbol]
        try:
            opt,why=choose_option(api,x,side,capital,cap)
            clock=datetime.now(IST)
            if (clock.hour,clock.minute)>=(15,10) or not fresh_candle(s['candle'],clock):continue
            if opt is None:messages.append(f'{symbol} {side}: SKIP — {why[:90]}');continue
            risk=opt['premium']*opt['lot']*sl_pct/100
            if risk>capital*risk_pct/100:
                messages.append(f'{symbol} {side}: SKIP — one-lot stop risk ₹{risk:,.0f} exceeds budget');continue
            exposure=0 if j.empty else pd.to_numeric(j.loc[j.status=='OPEN','cost'],errors='coerce').sum()
            if exposure+opt['premium']*opt['lot']>capital*.35:
                messages.append(f'{symbol} {side}: SKIP — 35% portfolio premium cap');continue
            row={'id':f'{clock:%Y%m%d%H%M%S}-{symbol}-{side}','symbol':symbol,'side':side,'status':'OPEN',
                 'signal_candle':s['candle'],'score':s['score'],'opened_at':clock.isoformat(timespec='seconds'),'closed_at':'',
                 'contract':opt['contract'],'expiry':opt['expiry'],'strike':opt['strike'],
                 'lot':opt['lot'],'lots':1,'entry_premium':opt['premium'],'last_bid':opt['bid'],
                 'cost':opt['premium']*opt['lot'],'stop_premium':opt['premium']*(1-sl_pct/100),
                 'target_premium':opt['premium']*(1+sl_pct/100*target_r),
                 'last_checked':now,'exit_premium':'','exit_reason':'','pnl':''}
            j=pd.concat([j,pd.DataFrame([row])],ignore_index=True)
            opened+=1;messages.append(f"PAPER OPEN {symbol} {side} {opt['contract']} @ ask ₹{opt['premium']:.2f}")
        except Exception as e:messages.append(f'{symbol} {side}: Dhan option lookup failed — {str(e)[:110]}')
    return j,messages

def fresh_candle(candle, clock):
    start=pd.Timestamp(candle)
    now=pd.Timestamp(clock)
    return start.date()==now.date() and now-pd.Timedelta(minutes=10)<start<=now-pd.Timedelta(minutes=5)


@st.cache_resource
def worker_pool():
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix='intraday')


def completed_slot(clock):
    t=pd.Timestamp(clock)
    return t.floor('5min')-pd.Timedelta(minutes=5)

def in_market(clock):
    return clock.weekday()<5 and (9,15)<=(clock.hour,clock.minute)<(15,30)

class CandleMemo:
    def __init__(self):
        self.lock=Lock()
        self.data=OrderedDict()

    def load(self, account, api, x):
        key=(account,str(x['sid']),x['segment'],x['instrument'])
        clock=datetime.now(IST)
        with self.lock:
            hit=self.data.get(key)
            if hit is not None:
                stamp,d=hit
                slot=completed_slot(clock)
                valid=(d.index[-1]==slot and stamp.date()==clock.date() if in_market(clock)
                       else not in_market(stamp) and stamp.date()==clock.date() and
                            (clock-stamp).total_seconds()<900)
                if valid:
                    self.data.move_to_end(key)
                    return d
        d=enrich(intraday_candles(api,x))
        with self.lock:
            self.data[key]=(clock,d)
            self.data.move_to_end(key)
            while len(self.data)>500:self.data.popitem(last=False)
        return d

_MEMO=CandleMemo()

def refresh_cycle(token,client,lookup,selected,j,threshold,capital,cap,stop,target,risk,auto,store,dirty,progress=None,journal_ready=True):
    """Worker has no Streamlit UI or session-state calls."""
    api=DhanAPI(token,client)
    results=[]; candles={}; errors=[]
    blocked=False
    account=hashlib.sha256(token.encode()).digest()
    attempted=0
    successful=0
    def fetch_symbol(symbol):
        # Each thread owns its HTTP session; Dhan's existing limiter is shared.
        local=DhanAPI(token,client)
        with local.session:
            return _MEMO.load(account,local,lookup[symbol])

    def record(symbol, fetch):
        nonlocal blocked, attempted, successful
        attempted += 1
        try:
            d=fetch()
            rows=signal(d,symbol,threshold)
            if symbol in selected_set:candles[symbol]=d
            results.extend(rows)
            successful += 1
        except Exception as e:
            msg=str(e)[:230]
            errors.append(f'{symbol}: {msg}')
            blocked=blocked or any(code in msg for code in ('DH-901','DH-902','HTTP 401','HTTP 403'))
        if progress is not None:
            progress.put((attempted,successful,symbol,errors[-1] if errors else ''))

    def scan_symbol(symbol):
        record(symbol, lambda: _MEMO.load(account,api,lookup[symbol]))

    def within_market(clock):
        return (9,15)<= (clock.hour,clock.minute) < (15,30) and clock.weekday()<5

    # Check exits before scanning the rest of the watchlist.
    open_symbols=set() if j.empty else set(j.loc[j.status=='OPEN','symbol'])
    old=j.copy(deep=True)
    exits=[]; entries=[]
    selected_set=set(selected)
    for symbol in sorted(open_symbols):
        scan_symbol(symbol)
        if blocked:break
    clock=datetime.now(IST)
    now=clock.isoformat(timespec='seconds')
    if within_market(clock) and journal_ready and not blocked:
        smap={(r['symbol'],r['side']):r for r in results if fresh_candle(r['candle'],clock)}
        j,exits=update_positions(j,api,lookup,smap,stop,target,now)
    elif open_symbols and not within_market(clock):
        errors.append('Outside market hours: open paper positions are not filled using closing quotes.')
    remaining=[symbol for symbol in dict.fromkeys(selected) if symbol not in open_symbols]
    if remaining and not blocked and not open_symbols:
        scan_symbol(remaining.pop(0))  # catch credentials failures before sending a batch
    if remaining and not blocked:
        from concurrent.futures import as_completed
        with ThreadPoolExecutor(max_workers=3,thread_name_prefix='dhan-candles') as pool:
            for offset in range(0,len(remaining),3):
                if blocked:break
                pending={pool.submit(fetch_symbol,s):s for s in remaining[offset:offset+3]}
                for future in as_completed(pending):
                    record(pending[future],future.result)
    clock=datetime.now(IST)
    now=clock.isoformat(timespec='seconds')
    priority={symbol:i for i,symbol in enumerate(dict.fromkeys([*sorted(open_symbols),*selected]))}
    results.sort(key=lambda row:(priority.get(row['symbol'],len(priority)),row['side']))
    if within_market(clock) and journal_ready and not blocked:
        entries_map={(r['symbol'],r['side']):r for r in results if r['symbol'] in selected
                     and fresh_candle(r['candle'],clock)}
        j,entries=create_entries(j,api,lookup,entries_map,capital,cap,stop,target,risk,auto and not dirty,now)
    if blocked:errors.append('Scan stopped early because Dhan rejected authentication or data access.')
    dirty=dirty or not j.equals(old)
    if dirty:
        try:
            errors.append(store.save(j))
            dirty=False
        except Exception as e:
            errors.append(f'Journal save failed: {e}. New entries paused until retry succeeds.')
    return ([r for r in results if r['symbol'] in selected],candles,j,errors+exits+entries,store.gist,dirty,now)


