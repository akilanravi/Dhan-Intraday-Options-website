"""Intraday calculations; completed 5-minute bars only. No broker orders."""
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from swing_core import DhanAPI, BASE, IST, ema, atr, parse_chain, select_contract


def intraday_candles(api, x, days=8, since=None):
    end = datetime.now(IST)
    start = since if since is not None else end - timedelta(days=days)
    payload = {'securityId': str(x['sid']), 'exchangeSegment': x['segment'], 'instrument': x['instrument'],
               'interval': '5', 'oi': False, 'fromDate': start.strftime('%Y-%m-%d %H:%M:%S'),
               'toDate': end.strftime('%Y-%m-%d %H:%M:%S')}
    result = api.post('/charts/intraday', payload)
    data = result.get('data', result)
    if not isinstance(data, dict) or not data.get('timestamp'):
        raise ValueError('No intraday candles returned')
    d = pd.DataFrame({k: data.get(v, []) for k, v in [('Open','open'),('High','high'),('Low','low'),('Close','close'),('Volume','volume')]})
    d.index = pd.to_datetime(data['timestamp'], unit='s', utc=True).tz_convert(IST)
    d = d.apply(pd.to_numeric, errors='coerce').dropna(subset=['Open','High','Low','Close']).sort_index()
    d = d[~d.index.duplicated(keep='last')]
    # API candle timestamps represent candle start. Never evaluate incomplete bar.
    now = pd.Timestamp.now(tz=IST)
    return d[d.index + pd.Timedelta(minutes=5) <= now]


def supertrend(d, period, multiplier):
    h,l,c = d.High,d.Low,d.Close
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    a = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    mid = (h+l)/2
    upper,lower = mid+multiplier*a,mid-multiplier*a
    # Preserve the recurrence exactly; NumPy arrays avoid per-bar pandas indexing.
    hi,lo=upper.to_numpy(copy=True),lower.to_numpy(copy=True)
    u,v=hi.copy(),lo.copy()
    close,midv,av=c.to_numpy(),mid.to_numpy(),a.to_numpy()
    direction=np.zeros(len(d),dtype=int)
    for i in range(1,len(d)):
        if np.isnan(av[i]):continue
        u[i]=hi[i] if np.isnan(u[i-1]) or hi[i]<u[i-1] or close[i-1]>u[i-1] else u[i-1]
        v[i]=lo[i] if np.isnan(v[i-1]) or lo[i]>v[i-1] or close[i-1]<v[i-1] else v[i-1]
        prior=direction[i-1]
        direction[i]=1 if close[i]>u[i-1] else -1 if close[i]<v[i-1] else (prior or (1 if close[i]>midv[i] else -1))
    return pd.Series(direction,index=d.index)


def enrich(d):
    d=d.copy()
    if len(d)<45: raise ValueError('Need at least 45 completed 5-minute candles')
    for n in (9,21): d[f'ema{n}']=ema(d.Close,n)
    typical=(d.High+d.Low+d.Close)/3
    session=d.index.date
    d['vwap']=(typical*d.Volume).groupby(session).cumsum()/d.Volume.groupby(session).cumsum().replace(0,np.nan)
    change=d.Close.diff(); up=change.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); dn=(-change.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    d['rsi']=(100-100/(1+up/dn.replace(0,np.nan))).mask((dn==0)&(up>0),100).mask((dn==0)&(up==0),50)
    d['volma']=d.Volume.shift(1).rolling(20).mean()
    d['st_entry']=supertrend(d,10,3)
    d['st_exit']=supertrend(d,7,2)
    prevday=d.groupby(session).agg({'High':'max','Low':'min','Close':'last'}).shift(1)
    piv=(prevday.High+prevday.Low+prevday.Close)/3
    bc=(prevday.High+prevday.Low)/2; tc=2*piv-bc
    d['tc']=pd.Series(np.maximum(tc,bc),index=prevday.index).reindex(session).to_numpy()
    d['bc']=pd.Series(np.minimum(tc,bc),index=prevday.index).reindex(session).to_numpy()
    d['pdh']=prevday.High.reindex(session).to_numpy(); d['pdl']=prevday.Low.reindex(session).to_numpy()
    # ORB: first 30 minutes 09:15-09:45 IST, only usable from 09:45 onward
    hh=[];ll=[]
    for day,g in d.groupby(session):
        opening=g.between_time('09:15','09:40')
        hi=opening.High.max() if len(opening)>=5 else np.nan
        lo=opening.Low.min() if len(opening)>=5 else np.nan
        hh.extend([hi]*len(g));ll.extend([lo]*len(g))
    d['orb_hi']=hh;d['orb_lo']=ll
    # previous candle breakout and retest of CPR boundaries
    d['break_up']=(d.Close>d.tc)&(d.Close.shift(1)<=d.tc.shift(1))
    d['break_dn']=(d.Close<d.bc)&(d.Close.shift(1)>=d.bc.shift(1))
    d['retest_up']=(d.Low<=d.tc)&(d.Close>d.tc)&(d.Close>d.Open)
    d['retest_dn']=(d.High>=d.bc)&(d.Close<d.bc)&(d.Close<d.Open)
    return d


def signal(d, symbol, threshold=7):
    r=d.iloc[-1]; prior=d.iloc[-2]
    results=[]
    for side,sgn in [('CE',1),('PE',-1)]:
        bull=sgn==1
        tests={
          'CPR trend': bool(r.Close>r.tc if bull else r.Close<r.bc),
          'CPR breakout/retest': bool(r.break_up or r.retest_up if bull else r.break_dn or r.retest_dn),
          'PDH / PDL': bool(r.Close>r.pdh if bull else r.Close<r.pdl),
          'EMA 9/21': bool(r.ema9>r.ema21 if bull else r.ema9<r.ema21),
          'VWAP': bool(r.Close>r.vwap if bull else r.Close<r.vwap),
          'RSI 14': bool(r.rsi>=55 if bull else r.rsi<=45),
          'Supertrend 10,3': bool(r.st_entry==sgn),
          'Volume 1.5x': bool(r.Volume>=1.5*r.volma and r.Volume>0),
          'NIFTY tide': None, # needs separately fetched NIFTY index; never silently count it
        }
        score=sum(v is True for v in tests.values())
        # Eight verifiable checks + one explicitly unavailable until NIFTY data loaded.
        structure=tests['CPR breakout/retest'] or tests['PDH / PDL']
        valid=score>=threshold and tests['Supertrend 10,3'] and tests['EMA 9/21'] and tests['VWAP'] and tests['Volume 1.5x'] and structure
        results.append({'symbol':symbol,'side':side,'score':score,'max_score':8,'eligible':bool(valid),
          'close':float(r.Close),'candle':str(d.index[-1]),'tests':tests,'exit_st':int(r.st_exit),
          'volume_source':d.attrs.get('volume_source','NSE equity'),
          'reverse_now':bool(r.st_exit==-sgn and prior.st_exit==sgn)})
    return results


def _daily_expiry(session, sid, segment, day):
    raw=session.expiries(sid,segment)
    expiries=raw.get('data',raw)
    if isinstance(expiries,dict): expiries=expiries.get('expiry',expiries.get('data',[]))
    dates=sorted(pd.Timestamp(v).date() for v in expiries)
    return next((v for v in dates if (v-day).days>=2),None)


def choose_option(api,x,side,capital,max_premium=35000,max_spread=5):
    now=datetime.now(IST).date()
    # Use cached expiry metadata where supplied. Live bid/ask is always fetched separately.
    exp=api.nearest_expiry(x['sid'],x['segment'],now) if hasattr(api,'nearest_expiry') else _daily_expiry(api,x['sid'],x['segment'],now)
    if exp is None: return None,'No eligible expiry'
    try: lot=int(float(x.get('lot',0) or 0))
    except (ValueError, OverflowError, TypeError): lot=0
    if lot<=0: return None,'Missing or invalid lot size in Dhan instrument master'
    spot,chain=parse_chain(api.chain(x['sid'],x['segment'],exp.isoformat()))
    if chain.empty: return None,'No option-chain quotes'
    p={'delta_lo':0.4,'delta_hi':0.75,'max_spread':max_spread,'lot':lot,'max_ask':max_premium/lot}
    selected,_,reason=select_contract(chain,side,spot,p)
    if selected is None: return None,reason
    ask,bid=float(selected['ask']),float(selected['bid'])
    return {'contract':f"{x['symbol']} {exp} {selected['strike']:g} {side}", 'expiry':exp.isoformat(),
            'strike':float(selected['strike']),'lot':lot,'premium':ask,'bid':bid,
            'delta':float(selected['delta']),'iv':float(selected['iv']),'spot':spot},''
