"""Dhan Intraday Options V1. Separate repository, paper-only, automatic in-session refresh."""
import io, math, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from queue import SimpleQueue
import pandas as pd
import requests
import streamlit as st
from intraday import intraday_candles,enrich,signal,choose_option
from swing_core import DhanAPI,load_universe,IST,parse_chain,select_contract
from underlying_backtest import render_backtest
from decision_panel import show_panel, load_quote, context, number
import hashlib

from paper_engine import PaperStore, universe, worker_pool, fresh_candle, refresh_cycle
from worker_store import cloud_journal, enabled

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
scope=st.radio('Scan scope',('Watchlist','All F&O and indices'),horizontal=True)
watchlist=st.multiselect('Scan F&O stocks and indices',options,default=[s for s in ['NIFTY','BANKNIFTY','RELIANCE','HDFCBANK'] if s in options])
selected=options if scope=='All F&O and indices' else watchlist
st.caption(f'{len(selected)} selected · Use Watchlist for quicker checks. Full-universe scans are paced to Dhan limits.')
ss=st.session_state
for key,value in {'results':[],'candles':{},'messages':[],'future':None,'checked':0.0,
                  'last_scan':'','scan_queue':None,'scan_progress':None,'scan_total':0,'dirty':False,'journal_error':'','gist_id':secrets.get('INTRADAY_GIST_ID','')}.items():
    ss.setdefault(key,value)
worker_mode=enabled(secrets.get('PAPER_WORKER_MODE',os.getenv('PAPER_WORKER_MODE','')) )
if 'journal' not in ss and not worker_mode:
    try:
        ss.journal=PaperStore(secrets.get('GITHUB_TOKEN',''),ss.gist_id).load()
    except Exception as e:
        ss.journal=pd.DataFrame()
        ss.journal_error=f'Journal could not be loaded: {e}. Paper updates disabled until the journal can be loaded.'

@st.fragment(run_every=3)
def dashboard():
    """Paint current data immediately. Submit/poll network work off the UI thread."""
    if worker_mode and time.monotonic()-ss.get('cloud_checked',-1000)>=15:
        ss.cloud_checked=time.monotonic()
        try:
            ss.journal,status=cloud_journal(secrets.get('GITHUB_TOKEN',''),ss.gist_id,'intraday').read()
            ss.journal_error=''
            ss.worker_status=status
        except Exception as e:
            ss.setdefault('journal',pd.DataFrame())
            ss.journal_error=f'Cannot read worker journal: {e}'
    if ss.scan_queue is not None:
        while not ss.scan_queue.empty():
            ss.scan_progress=ss.scan_queue.get_nowait()
    if ss.future is not None and ss.future.done():
        try:
            r,c,j,m,g,dirty,stamp=ss.future.result()
            if r:
                ss.results,ss.candles=r,c
            elif ss.results:
                m.insert(0, 'No new scanner data. Showing previous results. Old signals are not used for paper entries.')
            if not worker_mode:
                ss.journal,ss.gist_id,ss.dirty=j,g,dirty
            ss.messages,ss.last_scan=m,stamp
        except Exception as e:
            ss.messages=[f'Refresh failed: {str(e)[:180]}']
        finally:
            ss.future=None
    manual=st.button('🔄 REFRESH NOW',type='primary',use_container_width=True,
                     disabled=ss.future is not None)
    clock=datetime.now(IST)
    in_session=clock.weekday()<5 and (9,15)<=(clock.hour,clock.minute)<(15,30)
    if ss.future is None and (manual or (not worker_mode and in_session and time.monotonic()-ss.checked>=60)):
        ss.checked=time.monotonic()
        lookup={row.symbol:row.to_dict() for _,row in all_u.iterrows()}
        store=PaperStore(secrets.get('GITHUB_TOKEN',''),ss.gist_id)
        ss.scan_queue=SimpleQueue()
        ss.scan_progress=(0,0,'','')
        opened=[] if ss.journal.empty else list(ss.journal.loc[ss.journal.status=='OPEN','symbol'])
        ss.scan_total=len(set(selected)|set(opened))
        ss.future=worker_pool().submit(refresh_cycle,token,client,lookup,list(selected),
                                       ss.journal.copy(deep=True),threshold,capital,cap,stop,target,risk,
                                       auto,store,ss.dirty,ss.scan_queue,not worker_mode and not ss.journal_error)
    state='Updating in background' if ss.future is not None else ('Auto-refresh on' if in_session else 'Outside regular NSE hours')
    st.caption(f"{state} · Last completed refresh attempt: {ss.last_scan or '—'}")
    if ss.scan_progress is not None:
        done,ok,symbol,last_error=ss.scan_progress
        st.caption(f'Scan progress: {done}/{ss.scan_total} checked · {ok} with data · {done-ok} failed' + (f' · Last: {symbol}' if symbol else ''))
        if last_error:st.warning(last_error)
    if not in_session:
        st.caption('Refresh Now shows recent historical candles. No live entries or exits outside regular hours.')
    if ss.journal_error:
        st.error(ss.journal_error)
    if worker_mode:
        status=ss.get('worker_status',{})
        st.caption(f"Worker owns paper trading · Last worker check: {status.get('checked_at','not reported')} · {status.get('state','waiting')}")
        if status.get('notes'):st.caption(status['notes'][-1])
    render_dashboard()

BIAS_COLORS = {
    'Bullish': ('#75f0bd', '#103b32'),
    'Bearish': ('#ff9da5', '#48242d'),
    'Neutral': ('#ffe082', '#433716'),
}
LEGEND = '🟢 Bullish · 🔴 Bearish · 🟡 Neutral'

def signed_bias(values, reference=0):
    v = pd.to_numeric(values, errors='coerce')
    base = pd.to_numeric(reference, errors='coerce')
    result = pd.Series('Neutral', index=v.index)
    result.loc[v.gt(base)] = 'Bullish'
    result.loc[v.lt(base)] = 'Bearish'
    result.loc[v.isna() | pd.isna(base)] = 'Unavailable'
    return result

def color_css(label):
    if label not in BIAS_COLORS:
        return ''
    fg, bg = BIAS_COLORS[label]
    return f'color: {fg}; background-color: {bg}; font-weight: 600'

def styled_table(frame, groups):
    styles = pd.DataFrame('', index=frame.index, columns=frame.columns)
    for columns, bias in groups:
        for column in columns:
            styles[column] = bias.map(color_css)
    return frame.style.apply(lambda _: styles, axis=None)

def candle_table(df):
    out = df.copy()
    out['Candle bias'] = signed_bias(out['Close'], out['Open'])
    groups = [(['Open', 'High', 'Low', 'Close', 'Candle bias'], out['Candle bias'])]
    for field, label in [('st_entry', 'Entry ST bias'), ('st_exit', 'Exit ST bias')]:
        if field in out:
            out[label] = signed_bias(out[field])
            groups.append(([field, label], out[label]))
    return styled_table(out, groups)

CHECKS = ('CPR trend','CPR breakout/retest','PDH / PDL','EMA 9/21','VWAP','RSI 14',
          'Supertrend 10,3','Volume 1.5x','NIFTY tide')

def scan_frame(rows):
    data=[]
    for x in rows:
        checks=x.get('tests',{})
        row={'Symbol':x['symbol'],'Side':x['side'],'Score':x['score'],
             'Qualified on candle':bool(x['eligible']),
             'Setup bias':('Bullish' if x['side']=='CE' else 'Bearish') if x['eligible'] else 'Neutral'}
        for name in CHECKS:
            value=checks.get(name if name!='PDH / PDL' or name in checks else 'Camarilla/PDH-PDL')
            row[name]='✓' if value is True else '✗' if value is False else '—'
        row.update({'Close':round(x['close'],2),'Candle (IST)':x['candle'],
                    'Exit ST (7,2)':x.get('exit_st'),'Reversal':x.get('reverse_now')})
        data.append(row)
    return pd.DataFrame(data)

def scanner_style(df):
    checks={'✓':'Bullish','✗':'Bearish','—':'Neutral'}
    return styled_table(df,[(['Side','Setup bias'],df['Setup bias'])]+
                        [([name],df[name].map(checks)) for name in CHECKS])

def render_dashboard():
    r=st.session_state.results;j=st.session_state.journal
    m1,m2,m3,m4=st.columns(4)
    m1.metric('Scanned symbols',len(set(x['symbol'] for x in r)))
    m2.metric('Eligible CE',sum(x['eligible'] and x['side']=='CE' and fresh_candle(x['candle'],datetime.now(IST)) for x in r))
    m3.metric('Eligible PE',sum(x['eligible'] and x['side']=='PE' and fresh_candle(x['candle'],datetime.now(IST)) for x in r))
    m4.metric('Open paper trades',0 if j.empty else int((j.status=='OPEN').sum()))
    if ss.messages:
        with st.expander('Scan and paper-trading diagnostics', expanded=not r):
            for msg in ss.messages:st.write(msg)
    tabs=st.tabs(['1 · Market scanner','2 · Eligible signal cards','3 · Trade analysis','4 · Paper trading','5 · Conditions','6 · Trade Decision'])
    with tabs[0]:
        st.subheader('Intraday 5-minute scanner')
        if r:
            df=scan_frame(r).sort_values(['Qualified on candle','Score','Symbol'],ascending=[False,False,True])
            st.caption('✓ Passed · ✗ Not met · — Not verified. NIFTY tide is not scored. Historical rows are not live entries.')
            side_view=st.radio('Scanner side',('CE · Bullish','PE · Bearish'),horizontal=True)
            view=df[df['Side']==side_view[:2]]
            qualified=st.checkbox('Only show qualifying setups',value=False)
            if qualified:view=view[view['Qualified on candle']]
            if view.empty:
                st.info('No rows for this view.')
            else:
                st.dataframe(scanner_style(view),use_container_width=True,hide_index=True,
                             column_config={'Symbol':st.column_config.TextColumn(pinned=True),
                                            'Close':st.column_config.NumberColumn(format='%.2f')})
            st.download_button('Download scanner CSV (both sides)',df.to_csv(index=False),'intraday_scanner.csv')
        else:st.info('Select instruments and click REFRESH.')
    with tabs[1]:
        st.subheader('Eligible CE / PE signals')
        card_side=st.radio('Signal cards side',('CE · Bullish','PE · Bearish'),horizontal=True)
        now=datetime.now(IST)
        eligible=[x for x in r if x['side']==card_side[:2] and x['eligible'] and fresh_candle(x['candle'],now)]
        if not eligible:st.info('No confirmed eligible signals for this side in the latest scan.')
        for i in range(0,len(eligible),3):
            cols=st.columns(3)
            for col,x in zip(cols,eligible[i:i+3]):
                with col:
                    label='Bullish' if x['side']=='CE' else 'Bearish'
                    border=BIAS_COLORS[label][0]
                    st.markdown(f'''<div style="background:#10223a;border:2px solid {border};border-radius:17px;padding:18px;color:#eaf4ff">
                    <h3 style="color:{border}!important">{x['symbol']} · {x['side']} · {label}</h3>
                    <h2 style="color:#5de7ff!important">{x['score']}/8</h2><p>Underlying close ₹{x['close']:,.2f}</p>
                    <small>{x['candle']}</small></div>''',unsafe_allow_html=True)
                    with st.expander('Condition checklist'):
                        for name, value in x['tests'].items():
                            st.write(('✅' if value is True else '❌' if value is False else '🟡')+' '+name)
    with tabs[2]:
        st.subheader('Candles + EMA + VWAP')
        if st.session_state.candles:
            symbol=st.selectbox('Instrument',list(st.session_state.candles),key='chart_symbol')
            d=st.session_state.candles[symbol].tail(90)
            try:
                import plotly.graph_objects as go
                fig=go.Figure()
                direction = signed_bias(d['Close'], d['Open'])
                for label, (color, _) in BIAS_COLORS.items():
                    bars = d.loc[direction.eq(label)]
                    if bars.empty:
                        continue
                    fig.add_trace(go.Candlestick(
                        x=bars.index,open=bars.Open,high=bars.High,low=bars.Low,close=bars.Close,
                        name=label,increasing_line_color=color,decreasing_line_color=color))
                for name,color in [('ema9','#fbd34d'),('ema21','#f67ea4'),('vwap','#5be3ff')]:
                    fig.add_trace(go.Scatter(x=d.index,y=d[name],name=name.upper(),line={'color':color,'width':1.5}))
                fig.update_layout(template='plotly_dark',paper_bgcolor='#0b1424',plot_bgcolor='#0b1424',height=520,xaxis_rangeslider_visible=False,uirevision=symbol)
                st.plotly_chart(fig,use_container_width=True)
            except ImportError:st.line_chart(d[['Close','ema9','ema21','vwap']])
            st.caption(LEGEND + ' · Candles compare Close to Open. Supertrend cells show the indicator’s own direction.')
            st.dataframe(candle_table(d.tail(20)),use_container_width=True)
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
        st.subheader('Conditions — green passed / red failed / yellow unverified')
        if r:
            symbol=st.selectbox('Choose instrument',sorted(set(x['symbol'] for x in r)),key='cond_symbol')
            for x in [v for v in r if v['symbol']==symbol]:
                st.markdown(f"**{x['side']} — {x['score']}/8 — {'ELIGIBLE' if x['eligible'] else 'WATCH / SKIP'}**")
                for k,v in x['tests'].items():
                    st.markdown(('🟢' if v is True else '🔴' if v is False else '🟡')+' '+k+(' — not verified' if v is None else ''))
        st.caption('Combined entry requires ≥ threshold, ST(10,3), EMA9/21, VWAP, volume 1.5×, and CPR/PDH trigger. Exit: premium SL, 2R target, ST(7,2) reversal, opposing eligible signal or 15:20 IST. These are configurable research rules, not a verified profitable strategy.')

    with tabs[5]:
        st.subheader('Intraday trade decision')
        if not r:
            st.info('Run a Market Scanner scan first.')
        else:
            symbol=st.selectbox('Decision symbol', sorted({x['symbol'] for x in r}), key='idea_i_symbol')
            side=st.radio('Decision side',('CE','PE'),horizontal=True,key='idea_i_side')
            match=next((x for x in r if x['symbol']==symbol and x['side']==side),None)
            meta=all_u[all_u.symbol.eq(symbol)]
            if match is None or meta.empty:
                st.info('No matching scan row or Dhan instrument mapping.')
            else:
                idea={**meta.iloc[0].to_dict(),**match,'mode':'intraday',
                      'reason':f"{match['score']}/8 · Existing scanner eligibility",
                      'account':hashlib.sha256(token.encode()).hexdigest()}
                journal_ok=not ss.journal_error and (not worker_mode or bool(ss.get('worker_status')))
                policy=dict(capital=capital,cap=cap,risk=risk,stop=stop/100,target_r=target,slip=.005,
                    delta_lo=.4,delta_hi=.75,spread=5,min_days=2,exposure=35,max_open=3,
                    has_connection=bool(token and client),journal_ok=bool(journal_ok),
                    start=(9,45),end=(15,10),window='09:45–15:10 IST (regular weekdays)',worker_mode=worker_mode)
                checks=[('Fresh 5-minute signal',bool(fresh_candle(match['candle'],datetime.now(IST))),
                         'Only the latest completed 5-minute candle can be considered.')]
                def inspect(p):
                    api=DhanAPI(token,client)
                    notes=[]
                    d=ss.candles.get(symbol)
                    if d is not None and not d.empty:
                        levels=('orb_hi','pdh','tc') if side=='CE' else ('orb_lo','pdl','bc')
                        last=d.iloc[-1]
                        levels=[(k,number(last.get(k))) for k in levels]
                        ahead=[(k,v) for k,v in levels if v is not None and (v>match['close'] if side=='CE' else v<match['close'])]
                        if ahead:
                            name,level=min(ahead,key=lambda x:abs(x[1]-match['close']))
                            notes.append(f'Nearest tracked level ahead from signal close: {name} {level:,.2f} ({abs(level-match["close"]):,.2f} underlying points). This is not an option target.')
                        else:notes.append('No tracked level ahead from signal close. This does not establish a clear path to target.')
                    try:
                        gates,_=context(api,idea,p)
                    except Exception as exc:
                        gates=[('15-minute EMA trend',None,'No verified timeframe data · '+str(exc)[:130])]
                    def expiry(dates,today):
                        return next((d for d in sorted(pd.Timestamp(x).date() for x in dates)
                                     if (d-today).days>=p['min_days']),None)
                    try:return load_quote(api,idea,p,expiry,parse_chain,select_contract),None,gates,notes
                    except Exception as exc:return None,str(exc)[:230],gates,notes
                if worker_mode: st.caption('This panel uses dashboard settings. The independent worker follows worker.env and is not controlled by this panel.')
                show_panel(idea,policy,j,inspect,key='preflight_intraday',base_checks=checks,
                           critical_context=['15-minute EMA trend'],
                           exit_rules=['Premium stop or target at an observed Dhan bid.',
                                       'Supertrend (7,2) reversal or opposing eligible signal.',
                                       'End-of-day exit checks start at 15:20 IST; a valid bid is needed.'])


home, bt = st.tabs(['Intraday dashboard','Call / Put Backtest'])
with home:
    dashboard()
with bt:
    render_backtest(all_u,token,client,DhanAPI,'5 min')

