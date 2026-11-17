"""Dhan Intraday Options V1. Separate repository, paper-only, automatic in-session refresh."""
import hashlib, io, math, os, time
from threading import Lock, local
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import requests
import streamlit as st
from intraday import intraday_candles,enrich,signal,choose_option,_daily_expiry
from breakout import scan_opening_breakouts
from swing_core import DhanAPI,load_universe,IST
from scan_clock import BAR, fresh_candle, write_window, entry_window

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
                if f.get('truncated'):
                    raw=requests.get(f['raw_url'],headers=self.headers,timeout=20)
                    raw.raise_for_status()
                    data=raw.text
                else: data=f.get('content','')
                return read_csv(data)
            return pd.DataFrame()
        return read_csv(self.path.read_text(encoding='utf-8')) if self.path.exists() else pd.DataFrame()
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

def read_csv(data):
    if not data.strip():
        return pd.DataFrame()
    frame=pd.read_csv(io.StringIO(data),dtype={'id':str}).fillna('')
    if 'pnl' not in frame:frame['pnl']=''
    required={'status','symbol','side','opened_at','signal_candle','expiry','strike','lot','lots','cost','entry_premium'}
    if not required.issubset(frame.columns):
        raise ValueError('Paper journal is missing required columns')
    return frame


@st.cache_data(ttl=14400,show_spinner=False)
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

def update_positions(j,api,lookup,signals,sl_pct,target_r,clock_fn=lambda:datetime.now(IST)):
    if j.empty:return j,[]
    messages=[]
    for i,t in j[j.status=='OPEN'].iterrows():
        symbol=str(t.symbol)
        if not write_window(clock_fn()):break
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
            reversal=(bool(current.get('reverse_now')) and fresh_candle(current.get('candle'),clock_fn())) or (bool(opposite.get('eligible')) and fresh_candle(opposite.get('candle'),clock_fn()))
            # End of day flat; 15:20 IST cut-off
            clock=clock_fn()
            if not write_window(clock):
                messages.append(f'{symbol}: quote arrived outside market hours; left OPEN')
                continue
            eod=(clock.hour,clock.minute)>=(15,20) or str(t.opened_at)[:10] != clock.date().isoformat()
            reason='TARGET' if live>=target else 'STOP' if live<=stop else 'REVERSAL' if reversal else 'EOD' if eod else ''
            observed=clock.isoformat(timespec='seconds')
            j.at[i,'last_bid']=live;j.at[i,'last_checked']=observed
            if reason:
                qty=int(float(t.lot))*int(float(t.lots))
                # Estimated friction: ₹20 brokerage each side, sell-side STT and other charges approximated.
                gross=(live-entry)*qty; charges=40+0.002*(entry+live)*qty
                j.at[i,'pnl']=round(gross-charges,2)
                j.at[i,'status']='CLOSED';j.at[i,'exit_premium']=live;j.at[i,'exit_reason']=reason;j.at[i,'closed_at']=observed
                messages.append(f'{symbol} {side}: closed on {reason} at bid ₹{live:.2f} (snapshot)')
        except Exception as e:messages.append(f'{symbol}: quote unavailable — {str(e)[:90]}; left OPEN')
    return j,messages

def create_entries(j,api,lookup,signals,capital,cap,sl_pct,target_r,risk_pct,auto,
                   clock_fn=lambda:datetime.now(IST)):
    messages=[]
    clock=clock_fn()
    if not auto:return j,['Automatic entries are paused.']
    if not entry_window(clock):
        return j,['Outside 09:45–15:10 IST: no new entries.']
    candidates=[(key,s) for key,s in signals.items() if s['eligible'] and fresh_candle(s['candle'],clock)]
    if not candidates:
        return j,['No fresh CE/PE setup passed the technical filters.']
    checked=opened=0
    stop_lookups_at=time.monotonic()+48
    for (symbol,side),s in sorted(candidates,key=lambda z:(-z[1]['score'],*z[0])):
        clock=clock_fn()
        if not entry_window(clock) or not fresh_candle(s['candle'],clock):
            messages.append('Entry window or candle expired before a fill.');break
        if not j.empty:
            if (j.status=='OPEN').sum()>=3:
                messages.append('Open paper-position limit reached (3).');break
            if ((j.status=='OPEN')&(j.symbol==symbol)).any() or ((j.symbol==symbol)&(j.side==side)&(j.signal_candle==s['candle'])).any():
                continue
        if checked>=10 or time.monotonic()>=stop_lookups_at:
            messages.append('Quote-check time/candidate limit reached for this cycle.');break
        x=lookup[symbol]
        exposure=0 if j.empty else pd.to_numeric(j.loc[j.status=='OPEN','cost'],errors='coerce').sum()
        effective_cap=min(cap,capital*risk_pct/sl_pct,capital*.35-exposure)
        if effective_cap<=0:
            messages.append('No remaining premium budget under the risk/exposure limits.');break
        try:
            checked+=1
            opt,why=choose_option(api,x,side,capital,effective_cap)
            clock=clock_fn()
            if not entry_window(clock) or not fresh_candle(s['candle'],clock):
                messages.append(f'{symbol} {side}: candle expired before the quote could be used.');break
            if opt is None:messages.append(f'{symbol} {side}: SKIP — {why}');continue
            cost=opt['premium']*opt['lot']
            if not math.isfinite(cost) or not 0<cost<=effective_cap:
                messages.append(f'{symbol} {side}: SKIP — one-lot premium exceeds effective cap');continue
            row={'id':f'{clock:%Y%m%d%H%M%S}-{symbol}-{side}','symbol':symbol,'side':side,'status':'OPEN',
                 'signal_candle':s['candle'],'score':s['score'],'opened_at':clock.isoformat(timespec='seconds'),'closed_at':'',
                 'contract':opt['contract'],'expiry':opt['expiry'],'strike':opt['strike'],
                 'lot':opt['lot'],'lots':1,'entry_premium':opt['premium'],'last_bid':opt['bid'],
                 'cost':cost,'stop_premium':opt['premium']*(1-sl_pct/100),
                 'target_premium':opt['premium']*(1+sl_pct/100*target_r),
                 'last_checked':clock.isoformat(timespec='seconds'),'exit_premium':'','exit_reason':'','pnl':''}
            j=pd.concat([j,pd.DataFrame([row])],ignore_index=True)
            opened+=1;messages.append(f"PAPER OPEN {symbol} {side} {opt['contract']} @ ask ₹{opt['premium']:.2f}")
        except Exception as e:
            messages.append(f'{symbol} {side}: Dhan option lookup failed — {str(e)[:150]}')
    messages.insert(0, f'{len(candidates)} fresh eligible setups · {checked} option lookups · {opened} paper entries.')
    return j,messages


@st.cache_resource
def worker_pool():
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix='intraday')


class FastDhan(DhanAPI):
    def __init__(self, token, client):
        super().__init__(token,client)
        self.timeout=(3.05,5)
        self.attempts=2
        self.account=hashlib.sha256((str(client)+'|'+token).encode()).digest()

    def nearest_expiry(self,sid,segment,day):
        key=(self.account,sid,segment,day)
        with _EXP_LOCK:
            if key in _EXPIRIES:return _EXPIRIES[key]
        exp=_daily_expiry(self,sid,segment,day)
        if exp is not None:
            with _EXP_LOCK:
                _EXPIRIES[key]=exp
                if len(_EXPIRIES)>1000:_EXPIRIES.pop(next(iter(_EXPIRIES)))
        return exp

_EXP_LOCK=Lock()
_EXPIRIES={}


class CandleMemo:
    """Cache warm history, request only the recent tail for each new candle."""
    def __init__(self):
        self.lock=Lock()
        self.data=OrderedDict()

    def load(self, api, x, slot, force=False, clock_fn=lambda:datetime.now(IST)):
        key=(api.account,str(x['sid']),x['segment'],x['instrument'])
        clock=clock_fn()
        cutoff=clock-timedelta(days=8)
        with self.lock:
            old=self.data.get(key)
        if old is not None and not force and slot is not None and slot in old.index:
            return old.loc[(old.index>=cutoff)&(old.index<=slot)]
        # Overlap one known bar to tolerate inclusive/exclusive fromDate boundaries.
        # Pin the request end to the completed candle; backfill the gap after downtime.
        since=max(cutoff,old.index[-1]) if old is not None and not old.empty else None
        through=slot+BAR if slot is not None else None
        if since is not None and through is not None and since>=through:
            since=slot-BAR
        new=intraday_candles(api,x,since=since,through=through)
        raw=pd.concat([old,new]) if old is not None else new
        raw=raw[~raw.index.duplicated(keep='last')].sort_index().loc[lambda d:d.index>=cutoff]
        # Suppress pre-open/after-hours records, which would corrupt VWAP and prior-day levels.
        raw=raw.between_time('09:15','15:25')
        if raw.empty:raise ValueError('No completed candles returned')
        with self.lock:
            current=self.data.get(key)
            if current is None or raw.index[-1]>=current.index[-1]:
                self.data[key]=raw
                self.data.move_to_end(key)
                while len(self.data)>750:self.data.popitem(last=False)
        return raw if slot is None else raw.loc[raw.index<=slot]

_MEMO=CandleMemo()


def refresh_cycle(token,client,lookup,selected,j,threshold,capital,cap,stop,target,risk,auto,store,dirty,
                  progress=None,journal_ready=True,slot=None,allow_trades=True,on_signals=None,
                  clock_fn=lambda:datetime.now(IST)):
    """Pin all scores to one candle. Prioritize exits, then rank the complete batch."""
    started=time.monotonic()
    j=j.copy(deep=True)
    results={}; candles={}; errors=[]
    blocked=False
    attempted=successful=0
    selected=list(dict.fromkeys(selected))
    selected_set=set(selected)
    workers=local()
    sessions=[]
    session_lock=Lock()

    def fetch_symbol(symbol,retry=False):
        if not hasattr(workers,'api'):
            workers.api=FastDhan(token,client)
            with session_lock:sessions.append(workers.api.session)
        api=workers.api
        x=lookup[symbol]
        raw=_MEMO.load(api,x,slot,force=retry,clock_fn=clock_fn)
        if slot is not None and (raw.empty or raw.index[-1]!=slot):
            return None, []
        if x['instrument']=='INDEX' and raw.Volume.tail(21).sum()<=0 and str(x.get('fut_sid','')).isdigit():
            try:
                fut=_MEMO.load(api,{'sid':x['fut_sid'],'segment':'NSE_FNO','instrument':'FUTIDX'},slot,force=retry,clock_fn=clock_fn)
                raw=raw.assign(Volume=fut.Volume.reindex(raw.index))
                raw.attrs['volume_source']='NSE index futures proxy'
            except Exception:
                raw=raw.copy();raw.attrs['volume_source']='Index volume unavailable'
        elif x['instrument']=='INDEX':
            raw=raw.copy()
            raw.attrs['volume_source']='Index volume unavailable' if raw.Volume.tail(21).sum()<=0 else 'Dhan index'
        frame=enrich(raw)
        rows=signal(frame,symbol,threshold)
        # Scanner-only metadata. The existing paper eligibility is unchanged.
        def check_at_close(prefix, side):
            matching=next(r for r in signal(prefix,symbol,threshold) if r['side']==side)
            return {k:matching[k] for k in ('score','max_score','eligible','tests')}
        if symbol in selected_set and x['instrument']!='INDEX':
            rows[0]['opening_breakout_15m']=scan_opening_breakouts(frame,check_at_close)
        return frame, rows

    delayed=[]
    next_preview=12
    def record(symbol,fetch,retry=False):
        nonlocal blocked,attempted,successful,next_preview
        published=[]
        chart=None
        if not retry:attempted+=1
        try:
            d,rows=fetch()
            if d is None:
                if not retry:delayed.append(symbol)
                else:errors.append(f'{symbol}: Dhan did not return the scheduled closed candle')
            else:
                if symbol in selected_set:
                    chart=candles[symbol]=d.tail(90).copy()
                    detected=clock_fn()
                    for row in rows:
                        if slot is not None:
                            row['detected_at']=detected.isoformat(timespec='seconds')
                            row['detect_seconds']=round(max(0,(pd.Timestamp(detected)-(slot+BAR)).total_seconds()),1)
                        bar=d.iloc[-1]
                        row['ohlv']={k:(float(bar[k]) if math.isfinite(float(bar[k])) else None)
                                     for k in ('Open','High','Low','Volume')}
                    published=rows
                results[symbol]=rows
                successful+=1
        except Exception as e:
            msg=str(e)[:210]
            errors.append(f'{symbol}: {msg}')
            blocked=blocked or any(code in msg for code in ('DH-901','DH-902','HTTP 401','HTTP 403'))
        if progress is not None:progress.put((attempted,successful,symbol,errors[-1] if errors else '',published,chart))
        if on_signals is not None and time.monotonic()-started>=next_preview:
            preview=[r for sym in selected for r in results.get(sym,[])]
            if any(r['eligible'] for r in preview):
                next_preview = time.monotonic()-started+22
                try:
                    on_signals(preview,candles,[f'{successful}/{len(selected)} symbols checked; scan still running.'])
                except Exception as e:
                    errors.append(f'Live preview failed: {e}')

    old=j.copy(deep=True)
    open_symbols=set() if j.empty else set(j.loc[j.status=='OPEN','symbol'])
    exits=[]; entries=[]
    ordered=list(dict.fromkeys([*sorted(open_symbols & lookup.keys()),*selected]))
    trade_api=FastDhan(token,client)
    with trade_api.session:
        try:
            with ThreadPoolExecutor(max_workers=10,thread_name_prefix='dhan-candles') as pool:
                priority=[s for s in ordered if s in open_symbols and s in lookup]
                def run_batch(symbols,retry=False):
                    pending={}
                    todo=iter(symbols)
                    def refill():
                        while len(pending)<10 and not blocked and time.monotonic()-started<155:
                            sym=next(todo,None)
                            if sym is None:break
                            pending[pool.submit(fetch_symbol,sym,retry)]=sym
                    refill()
                    while pending:
                        done,_=wait(pending,return_when=FIRST_COMPLETED)
                        for f in done:record(pending.pop(f),f.result,retry)
                        refill()
                run_batch(priority)
                priority_set=set(priority)
                run_batch([s for s in ordered if s not in priority_set])
                # Publish fresh first-pass rows before retrying missing symbols in worker mode.
                if delayed and on_signals is not None and results:
                    try:
                        on_signals([r for sym in selected for r in results.get(sym,[])],candles,
                                   [f'{successful}/{len(ordered)} symbols after first pass; retrying delayed candles.'])
                    except Exception as e:
                        errors.append(f'First-pass publish failed: {e}')
                run_batch(delayed.copy(),retry=True)
        finally:
            for session in sessions:session.close()
        clock=clock_fn()
        out=[r for sym in selected for r in results.get(sym,[])]
        wanted=len(ordered)
        summary=(f'{successful}/{wanted} symbols returned the requested candle · '
                 f'{attempted} attempted · {time.monotonic()-started:.1f} seconds (candle scan only).')
        if attempted<wanted and not blocked:errors.append('Candle scan time budget reached. Unfinished symbols will be checked on the next candle.')
        if blocked:errors.insert(0,'Dhan rejected authentication or data access. Check token and Data API entitlement.')
        if on_signals is not None:
            try:
                on_signals(out,candles,[summary,*errors])
            except Exception as e:
                errors.append(f'Early signal publish failed: {e}')
        if write_window(clock) and allow_trades and journal_ready and open_symbols:
            smap={(r['symbol'],r['side']):r for rows in results.values() for r in rows if fresh_candle(r['candle'],clock)}
            j,exits=update_positions(j,trade_api,lookup,smap,stop,target,clock_fn)
        if not journal_ready:entries=['Paper journal unavailable. Scanning continues, but simulation is paused.']
        elif not allow_trades:entries=['This is a read-only historical scan.']
        elif dirty:entries=['Paper journal has an unsaved update. New entries paused until it is saved.']
        elif not blocked:
            entries_map={(r['symbol'],r['side']):r for r in out if fresh_candle(r['candle'],clock)}
            j,entries=create_entries(j,trade_api,lookup,entries_map,capital,cap,stop,target,risk,
                                     auto,clock_fn)
    dirty=dirty or not j.equals(old)
    if dirty:
        try:
            errors.append(store.save(j))
            dirty=False
        except Exception as e:
            errors.append(f'Journal save failed: {e}. New entries paused until retry succeeds.')
    stamp=clock_fn().isoformat(timespec='seconds')
    return (out,candles,j,[summary,*exits,*entries,*errors],store.gist,dirty,stamp)
