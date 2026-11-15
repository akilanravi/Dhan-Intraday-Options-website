"""Dhan Intraday Options V1. Separate repository, paper-only, automatic in-session refresh."""
import io, math, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import requests
import streamlit as st
from intraday import intraday_candles,enrich,signal,choose_option
from swing_core import DhanAPI,load_universe,IST

st.set_page_config(page_title='DHAN QUANT · INTRADAY',layout='wide',page_icon='📈')
st.markdown('''<style>
.stApp{background:linear-gradient(145deg,#060b17,#101d35);color:#e9f3ff}
h1,h2,h3{color:#eaf4ff!important}p,label,.stMarkdown{color:#d6e6ff}
[data-testid="stMetric"]{background:#10223c;border:1px solid #2b4769;border-radius:16px;padding:14px}
[data-testid="stMetricValue"]{color:#4be4ff}
.stButton button{background:linear-gradient(100deg,#1459d9,#8742c6);color:white;border-radius:11px}
[data-testid="stDataFrame"]{border:1px solid #294261;border-radius:12px}
/* Keep Streamlit’s retained widgets at full contrast while replacements render. */
[data-stale="true"], .stale-element {opacity:1!important;transition:none!important;animation:none!important}
</style>''',unsafe_allow_html=True)

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


def refresh_cycle(token,client,lookup,selected,j,threshold,capital,cap,stop,target,risk,auto,store,dirty):
    """Worker has no Streamlit UI or session-state calls."""
    api=DhanAPI(token,client)
    results=[]; candles={}; errors=[]
    def scan_symbol(symbol):
        try:
            d=enrich(intraday_candles(api,lookup[symbol]))
            if symbol in selected: candles[symbol]=d
            results.extend(signal(d,symbol,threshold))
        except Exception as e:
            errors.append(f'{symbol}: {str(e)[:130]}')

    def within_market(clock):
        return (9,15)<= (clock.hour,clock.minute) < (15,30) and clock.weekday()<5

    # Check exits before scanning the rest of the watchlist.
    open_symbols=set() if j.empty else set(j.loc[j.status=='OPEN','symbol'])
    old=j.copy(deep=True)
    exits=[]; entries=[]
    for symbol in sorted(open_symbols):
        scan_symbol(symbol)
    clock=datetime.now(IST)
    now=clock.isoformat(timespec='seconds')
    if within_market(clock):
        smap={(r['symbol'],r['side']):r for r in results if fresh_candle(r['candle'],clock)}
        j,exits=update_positions(j,api,lookup,smap,stop,target,now)
    elif open_symbols:
        errors.append('Outside market hours: open paper positions are not filled using closing quotes.')
    for symbol in selected:
        if symbol not in open_symbols:
            scan_symbol(symbol)
    clock=datetime.now(IST)
    now=clock.isoformat(timespec='seconds')
    if within_market(clock):
        entries_map={(r['symbol'],r['side']):r for r in results if r['symbol'] in selected
                     and fresh_candle(r['candle'],clock)}
        j,entries=create_entries(j,api,lookup,entries_map,capital,cap,stop,target,risk,auto and not dirty,now)
    dirty=dirty or not j.equals(old)
    if dirty:
        try:
            errors.append(store.save(j))
            dirty=False
        except Exception as e:
            errors.append(f'Journal save failed: {e}. New entries paused until retry succeeds.')
    return ([r for r in results if r['symbol'] in selected],candles,j,errors+exits+entries,store.gist,dirty,now)


st.title('DHAN QUANT · INTRADAY OPTIONS TERMINAL')
st.caption('Dhan only · 5-minute completed candles · CE/PE · no Telegram · no real broker orders')
try: secrets=dict(st.secrets)
except Exception: secrets={}
with st.sidebar:
    st.header('Dhan connection')
    token=st.text_input('Access token',value=secrets.get('DHAN_ACCESS_TOKEN',''),type='password')
    client=st.text_input('Client ID',value=secrets.get('DHAN_CLIENT_ID',''),type='password')
    capital=st.number_input('Paper capital ₹',min_value=10000,value=100000,step=10000)
    cap=st.number_input('Maximum premium per trade ₹',min_value=1000,value=35000,step=1000)
    risk=st.slider('Risk per trade %',0.5,5.0,1.0,0.5)
    stop=st.slider('Premium stop-loss %',5,40,20,1)
    target=st.slider('Target multiple (R)',1.0,4.0,2.0,.5)
    threshold=st.slider('Minimum technical score / 8',5,8,7)
    st.caption('9th legacy condition (NIFTY tide) is not scored until a verified index feed is integrated.')
    auto=st.checkbox('Automatic paper entries',value=True)
    st.caption('Automatic checks every 60 seconds while this dashboard is connected during market hours. Turning off entries does not disable exits.')

if not token or not client:
    st.warning('Enter DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID in sidebar or Streamlit Secrets.');st.stop()

try: all_u=universe()
except Exception as e:st.error(f'Dhan instrument master error: {e}');st.stop()
options=all_u.symbol.astype(str).tolist()
selected=st.multiselect('Scan F&O stocks and indices',options,default=[s for s in ['NIFTY','BANKNIFTY','RELIANCE','HDFCBANK'] if s in options])
ss=st.session_state
for key,value in {'results':[],'candles':{},'messages':[],'future':None,'checked':0.0,
                  'last_scan':'','dirty':False,'journal_error':'','gist_id':secrets.get('INTRADAY_GIST_ID','')}.items():
    ss.setdefault(key,value)
if 'journal' not in ss:
    try:
        ss.journal=PaperStore(secrets.get('GITHUB_TOKEN',''),ss.gist_id).load()
    except Exception as e:
        ss.journal=pd.DataFrame()
        ss.journal_error=f'Journal could not be loaded: {e}. Paper updates disabled until the journal can be loaded.'

@st.fragment(run_every=3)
def dashboard():
    """Paint current data immediately. Submit/poll network work off the UI thread."""
    if ss.future is not None and ss.future.done():
        try:
            r,c,j,m,g,dirty,stamp=ss.future.result()
            if r:
                ss.results,ss.candles=r,c
            elif ss.results:
                m.insert(0, 'No new scanner data. Showing previous results. Old signals are not used for paper entries.')
            ss.journal,ss.messages,ss.gist_id,ss.dirty,ss.last_scan=j,m,g,dirty,stamp
        except Exception as e:
            ss.messages=[f'Refresh failed: {str(e)[:180]}']
        finally:
            ss.future=None
    manual=st.button('🔄 REFRESH NOW',type='primary',use_container_width=True,
                     disabled=ss.future is not None)
    clock=datetime.now(IST)
    in_session=clock.weekday()<5 and (9,15)<=(clock.hour,clock.minute)<(15,30)
    if ss.future is None and (manual or (in_session and time.monotonic()-ss.checked>=60)):
        ss.checked=time.monotonic()
        lookup={row.symbol:row.to_dict() for _,row in all_u.iterrows()}
        if not ss.journal_error:
            store=PaperStore(secrets.get('GITHUB_TOKEN',''),ss.gist_id)
            ss.future=worker_pool().submit(refresh_cycle,token,client,lookup,list(selected),
                                           ss.journal.copy(deep=True),threshold,capital,cap,stop,target,risk,
                                           auto,store,ss.dirty)
    state='Updating in background' if ss.future is not None else ('Auto-refresh on' if in_session else 'Outside regular NSE hours')
    st.caption(f"{state} · Last completed refresh attempt: {ss.last_scan or '—'}")
    if ss.journal_error:
        st.error(ss.journal_error)
    render_dashboard()

def render_dashboard():
    r=st.session_state.results;j=st.session_state.journal
    m1,m2,m3,m4=st.columns(4)
    m1.metric('Scanned symbols',len(set(x['symbol'] for x in r)))
    m2.metric('Eligible CE',sum(x['eligible'] and x['side']=='CE' for x in r))
    m3.metric('Eligible PE',sum(x['eligible'] and x['side']=='PE' for x in r))
    m4.metric('Open paper trades',0 if j.empty else int((j.status=='OPEN').sum()))
    for msg in st.session_state.messages[-12:]:st.caption(msg)
    tabs=st.tabs(['1 · Market scanner','2 · Eligible signal cards','3 · Trade analysis','4 · Paper trading','5 · Conditions'])
    with tabs[0]:
        st.subheader('Intraday 5-minute scanner')
        if r:
            df=pd.DataFrame([{k:v for k,v in x.items() if k not in ('tests',)} for x in r]).sort_values(['eligible','score'],ascending=False)
            st.dataframe(df,use_container_width=True,hide_index=True)
            st.download_button('Download scanner CSV',df.to_csv(index=False),'intraday_scanner.csv')
        else:st.info('Select instruments and click REFRESH.')
    with tabs[1]:
        st.subheader('Eligible CE / PE signals')
        eligible=[x for x in r if x['eligible']]
        if not eligible:st.info('No confirmed eligible signals in the latest scan.')
        for i in range(0,len(eligible),3):
            cols=st.columns(3)
            for col,x in zip(cols,eligible[i:i+3]):
                with col:
                    border='#21e4a3' if x['side']=='CE' else '#ff648e'
                    st.markdown(f'''<div style="background:#10223a;border:2px solid {border};border-radius:17px;padding:18px;color:#eaf4ff">
                    <h3 style="color:{border}!important">{x['symbol']} · {x['side']}</h3>
                    <h2 style="color:#5de7ff!important">{x['score']}/8</h2><p>Underlying close ₹{x['close']:,.2f}</p>
                    <small>{x['candle']}</small></div>''',unsafe_allow_html=True)
    with tabs[2]:
        st.subheader('Candles + EMA + VWAP')
        if st.session_state.candles:
            symbol=st.selectbox('Instrument',list(st.session_state.candles),key='chart_symbol')
            d=st.session_state.candles[symbol].tail(90)
            try:
                import plotly.graph_objects as go
                fig=go.Figure()
                fig.add_trace(go.Candlestick(x=d.index,open=d.Open,high=d.High,low=d.Low,close=d.Close,name='OHLC',
                    increasing_line_color='#1ee5a0',decreasing_line_color='#ff5c86'))
                for name,color in [('ema9','#fbd34d'),('ema21','#f67ea4'),('vwap','#5be3ff')]:
                    fig.add_trace(go.Scatter(x=d.index,y=d[name],name=name.upper(),line={'color':color,'width':1.5}))
                fig.update_layout(template='plotly_dark',paper_bgcolor='#0b1424',plot_bgcolor='#0b1424',height=520,xaxis_rangeslider_visible=False,uirevision=symbol)
                st.plotly_chart(fig,use_container_width=True)
            except ImportError:st.line_chart(d[['Close','ema9','ema21','vwap']])
            st.dataframe(d.tail(20).round(2),use_container_width=True)
        else:st.info('Scan first to view candles.')
    with tabs[3]:
        st.subheader('Automatic paper journal')
        st.caption('Entry at Dhan ask, exits at observed Dhan bid. Prices are snapshots. Missed SL/target between refreshes cannot be reconstructed.')
        if j.empty:st.info('No paper trades yet.')
        else:
            st.markdown('**Open trades**');st.dataframe(j[j.status=='OPEN'],use_container_width=True,hide_index=True)
            st.markdown('**Closed trades**');st.dataframe(j[j.status=='CLOSED'],use_container_width=True,hide_index=True)
            closed=j[j.status=='CLOSED'].copy()
            if len(closed):
                p=pd.to_numeric(closed.pnl,errors='coerce').fillna(0)
                a,b,c=st.columns(3);a.metric('Net P&L ₹',f'{p.sum():,.2f}');b.metric('Win rate',f'{(p>0).mean()*100:.1f}%');c.metric('Closed trades',len(closed))
            st.download_button('Download paper journal CSV',j.to_csv(index=False),'intraday_paper_trades.csv')
        if secrets.get('GITHUB_TOKEN') and ss.gist_id and not secrets.get('INTRADAY_GIST_ID'):
            st.warning(f'Add this journal ID to Streamlit Secrets to reconnect after restart: INTRADAY_GIST_ID = "{ss.gist_id}"')
        if not secrets.get('GITHUB_TOKEN'):st.warning('Persistent journal is NOT configured. Local Streamlit Cloud storage is temporary. Add GITHUB_TOKEN and INTRADAY_GIST_ID.')
    with tabs[4]:
        st.subheader('Conditions — green passed / red failed')
        if r:
            symbol=st.selectbox('Choose instrument',sorted(set(x['symbol'] for x in r)),key='cond_symbol')
            for x in [v for v in r if v['symbol']==symbol]:
                st.markdown(f"**{x['side']} — {x['score']}/8 — {'ELIGIBLE' if x['eligible'] else 'WATCH / SKIP'}**")
                for k,v in x['tests'].items():
                    st.markdown(('🟢' if v is True else '🔴' if v is False else '⚪')+' '+k+(' — not verified' if v is None else ''))
        st.caption('Combined entry requires ≥ threshold, ST(10,3), EMA9/21, VWAP, volume 1.5×, and CPR/PDH trigger. Exit: premium SL, 2R target, ST(7,2) reversal, opposing eligible signal or 15:20 IST. These are configurable research rules, not a verified profitable strategy.')


dashboard()
