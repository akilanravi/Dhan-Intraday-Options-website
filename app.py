"""Dhan Intraday Options. Five-minute, full-universe paper-only dashboard."""
import hashlib
import html
import os
import uuid
from datetime import datetime
import pandas as pd
import streamlit as st
from swing_core import DhanAPI,IST,parse_chain,select_contract
from underlying_backtest import render_backtest
from decision_panel import show_panel, load_quote, context, number
from paper_engine import universe, fresh_candle
import dashboard_runtime as scanner_runtime
from scan_clock import BAR, timestamp, scheduled_slot, regular_day, completed_slot, write_window
from paper_report import performance_by_symbol
from breakout_view import render_breakouts
from intraday import choose_option
from signal_history import times_for
from dashboard_auth import require_password, is_authenticated, read_secret

st.set_page_config(page_title='DHAN QUANT · INTRADAY',layout='wide',page_icon='📈')
require_password('Dhan Intraday')
st.markdown('''<style>
.stApp{background:linear-gradient(145deg,#060b17,#101d35);color:#e9f3ff}
h2,h3{color:#eaf4ff!important}p,label,.stMarkdown{color:#d6e6ff}
h1{background:linear-gradient(90deg,#6ee7ff,#b69bff,#ff76c7);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
[data-testid="stMetric"]{background:#10223c;border:1px solid #2b4769;border-radius:16px;padding:14px}
[data-testid="stMetricValue"]{color:#4be4ff}
.stButton button{background:linear-gradient(100deg,#1459d9,#8742c6);color:white;border-radius:11px}
[data-testid="stDataFrame"]{border:1px solid #294261;border-radius:12px}
/* Keep Streamlit’s retained widgets at full contrast while replacements render. */
[data-stale="true"], .stale-element {opacity:1!important;transition:none!important;animation:none!important}
</style>''',unsafe_allow_html=True)

st.title('DHAN QUANT · INTRADAY OPTIONS TERMINAL')
st.caption('Dhan only · 5-minute completed candles · CE/PE · no Telegram · no real broker orders')
st.caption('Build 5.4 · Live signal timestamps · 15-minute opening breakouts')

RUNTIME_API_REQUIRED=5
UPDATE_NOTE=('Scanner files are out of sync. Replace the project files together from the same ZIP '
             'in one commit, then use Manage app → Reboot app. Keep your existing secrets.')
if getattr(scanner_runtime,'DASHBOARD_RUNTIME_API',None)!=RUNTIME_API_REQUIRED:
    st.error(UPDATE_NOTE)
    st.stop()
secrets={key:read_secret(key) for key in ("GITHUB_TOKEN","INTRADAY_GIST_ID","NSE_CLOSED_DATES")}
# This edition uses Streamlit as the only credential holder and scanner host.
worker_mode=False
token=read_secret('DHAN_ACCESS_TOKEN')
client=read_secret('DHAN_CLIENT_ID')
with st.sidebar:
    st.header('Dhan connection')
    st.caption('Dhan credentials configured on server.' if token and client else 'Dhan credentials missing in Streamlit Secrets.')
    if worker_mode:
        st.caption('The independent worker owns scanning and paper entries. Edit worker.env to change its settings.')
        capital,cap,risk,stop,target,threshold,auto=100000,35000,5.0,20,2.0,7,True
    else:
        capital=st.number_input('Paper capital ₹',min_value=10000,value=100000,step=10000)
        cap=st.number_input('Maximum premium per trade ₹',min_value=1000,value=35000,step=1000,max_value=35000)
        risk=st.slider('Risk per trade %',0.5,5.0,5.0,0.5)
        stop=st.slider('Premium stop-loss %',5,40,20,1)
        target=st.slider('Target multiple (R)',1.0,4.0,2.0,.5)
        threshold=st.slider('Minimum technical score / 8',5,8,7)
        auto=st.checkbox('Automatic paper entries',value=True)
        st.caption(f'Current one-lot premium ceiling before existing exposure: ₹{min(cap,capital*risk/stop,capital*.35):,.0f}. This includes the planned premium-stop risk limit.')
    st.caption('The legacy NIFTY tide condition is not scored.')

if not worker_mode and (not token or not client):
    st.warning('The owner must configure DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID in Streamlit Secrets.')
    st.stop()

try:
    all_u=universe()
except Exception as e:
    st.error(f'Dhan instrument master error: {e}')
    st.stop()

lookup={x['symbol']:x for x in all_u.to_dict('records')}
priority=()
if not worker_mode:
    with st.sidebar:
        priority=st.multiselect('Scan these symbols first',sorted(lookup),
            default=[s for s in ('NIFTY','BANKNIFTY','RELIANCE','HDFCBANK') if s in lookup],
            max_selections=20,help='These results appear first. All NSE F&O underlyings still get scanned.')
ss=st.session_state
ss.setdefault('runtime_owner',uuid.uuid4().hex)
github=str(secrets.get('GITHUB_TOKEN',''))
gist=str(secrets.get('INTRADAY_GIST_ID',''))
params=dict(threshold=threshold,capital=capital,cap=cap,stop=stop,target=target,risk=risk,auto=auto)
settings={'NSE_CLOSED_DATES':secrets.get('NSE_CLOSED_DATES',os.getenv('NSE_CLOSED_DATES',''))}
runtime=scanner_runtime.get_runtime((RUNTIME_API_REQUIRED,worker_mode,gist,hashlib.sha256((client+'|'+token).encode()).hexdigest()),worker_mode,github,gist,ss.runtime_owner)
if getattr(runtime,'api_version',None)!=RUNTIME_API_REQUIRED:
    st.error(UPDATE_NOTE)
    st.stop()


def dashboard():
    """Clock and publishing run even when the market-scanner tab is not selected."""
    clock=datetime.now(IST)
    configured_market=regular_day(clock,settings)
    grace=configured_market and (9,20)<=(clock.hour,clock.minute)<(15,35) and scheduled_slot(clock,settings) is None
    manual=st.button('Pull latest worker results' if worker_mode else '🔄 REFRESH NOW',
                     type='primary')
    data=runtime.step(clock,settings,manual,lookup,token,client,params,ss.runtime_owner,priority=priority)
    if data.notice:st.caption(data.notice)
    elif manual and grace and not worker_mode:st.caption('Waiting briefly for Dhan to publish the new closed candle.')
    state=('Scanning in background' if data.future is not None else
           data.status.get('state','Waiting for worker') if worker_mode else
           'Background 5-minute scans on' if scheduled_slot(clock,settings) is not None or grace else
           'Waiting for the next regular NSE session / candle')
    st.caption(f"{state} · Full universe: {len(lookup)} NSE F&O underlyings including indices.")
    end=candle_close(data.slot) if data.slot is not None else '—'
    if scheduled_slot(clock,settings) is not None:
        st.caption(f'Latest expected candle close: {candle_close(completed_slot(clock))}')
    if data.partial and data.preview_slot is not None:
        st.caption(f'Receiving candle closed at {candle_close(data.preview_slot)}. Missing symbols are not filled with older data.')
    checked=pd.Timestamp(data.stamp).tz_convert(IST).strftime('%d %b %H:%M:%S IST') if data.stamp else '—'
    st.caption(f'Published candle close: {end} · Last snapshot update: {checked}')
    if worker_mode and data.status.get('phase')=='preview':
        st.caption('Live worker preview. Signals are available before paper checks and full chart history finish.')
    if data.progress is not None:
        done,ok,symbol,_=data.progress
        st.caption(f'Scan progress {done}/{len(lookup)} checked · {ok} with candle data'+(f' · Last: {symbol}' if symbol else ''))
    if worker_mode and data.status:
        st.caption(f"Worker coverage {data.status.get('scanned','—')}/{data.status.get('requested','—')} · Worker entry setting: {'on' if data.policy.get('auto') else 'off / not reported'}")
    if not worker_mode and not data.controller:
        st.caption('Another dashboard session supplied the current paper settings. Refresh Now can take control for the next scan.')
    if data.error:
        st.error(data.error)
    if data.notes:
        with st.expander('Scan and paper-trading diagnostics',expanded=not data.rows):
            for note in data.notes[:40]:
                st.text(note)
    return data

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

def candle_range(value):
    start=timestamp(value)
    return f'{start:%d %b %H:%M}–{start+BAR:%H:%M} IST'

def candle_close(value):
    return f'{timestamp(value)+BAR:%d %b %H:%M} IST'

def scan_frame(rows, candles=None, history=None):
    data=[]
    for x in rows:
        checks=x.get('tests',{})
        row={'Symbol':x['symbol'],'Side':x['side'],'Score':x['score'],
             'Qualified on candle':bool(x['eligible']),
             'Qualified closes (IST)':times_for(history or {},x),
             'Detected (IST)':pd.Timestamp(x['detected_at']).strftime('%H:%M:%S') if x.get('detected_at') else '',
             'Detection delay (s)':x.get('detect_seconds'),
             'Setup bias':('Bullish' if x['side']=='CE' else 'Bearish') if x['eligible'] else 'Neutral',
             'Volume source':x.get('volume_source','—')}
        for name in CHECKS:
            value=checks.get(name if name!='PDH / PDL' or name in checks else 'Camarilla/PDH-PDL')
            row[name]='✓' if value is True else '✗' if value is False else '—'
        raw=(candles or {}).get(x['symbol'])
        bar=x.get('ohlv') or (raw.iloc[-1] if raw is not None and not raw.empty and raw.index[-1]==timestamp(x['candle']) else {})
        row.update({'Open':bar.get('Open'),'High':bar.get('High'),'Low':bar.get('Low'),
                    'Close':round(x['close'],2),'Volume':bar.get('Volume'),
                    'Candle close (IST)':candle_close(x['candle']),
                    'Candle start (IST)':x['candle'],
                    'Exit ST (7,2)':x.get('exit_st'),'Reversal':x.get('reverse_now')})
        data.append(row)
    return pd.DataFrame(data)

def scanner_table(rows,candles,history=None):
    return scan_frame(rows,candles,history).sort_values(['Qualified on candle','Score','Symbol'],ascending=[False,False,True])

def scanner_style(df):
    checks={'✓':'Bullish','✗':'Bearish','—':'Neutral'}
    return styled_table(df,[(['Side','Setup bias'],df['Setup bias'])]+
                        [([name],df[name].map(checks)) for name in CHECKS])

def render_dashboard(tab,live):
    r,j=live.rows,live.journal
    now=datetime.now(IST)
    visible_slot=live.preview_slot if live.partial else live.slot
    table_live=regular_day(now,settings) and write_window(now) and visible_slot is not None and visible_slot==completed_slot(now)
    if live.partial and tab != 3:
        st.caption('Partial scan. Results update as Dhan responds. Paper entries are evaluated after the batch, not from this preview.')
    if tab not in (3,7) and r and not table_live:
        st.caption('Showing the last available snapshot. These are not current live-entry signals.')
    if tab == 0:
        m1,m2,m3,m4=st.columns(4)
        m1.metric('Scanned symbols',len(set(x['symbol'] for x in r)))
        m2.metric('Eligible CE',sum(table_live and x['eligible'] and x['side']=='CE' for x in r))
        m3.metric('Eligible PE',sum(table_live and x['eligible'] and x['side']=='PE' for x in r))
        m4.metric('Open paper trades',0 if j.empty else int((j.status=='OPEN').sum()))
        st.subheader('Latest completed 5-minute data')
        if r:
            df=scanner_table(r,live.candles,live.signal_history)
            st.caption('✓ Passed · ✗ Not met · — Not verified. NIFTY tide is not scored. Historical rows are not live entries.')
            side_view=st.radio('Scanner side',('Both','CE · Bullish','PE · Bearish'),horizontal=True,key='scanner_side_52')
            view=df if side_view=='Both' else df[df['Side']==side_view[:2]]
            symbols=st.multiselect('Filter displayed symbols (scan still covers all F&O)',sorted(lookup))
            if symbols:
                view=view[view.Symbol.isin(symbols)]
                missing=sorted(set(symbols)-set(df.Symbol))
                if missing:st.info('No candle in this snapshot for '+', '.join(missing))
            qualified=st.checkbox('Only show qualifying setups',value=False)
            if qualified:view=view[view['Qualified on candle']]
            if view.empty:
                st.info('No rows for this view.')
            else:
                st.dataframe(scanner_style(view) if len(view)<=80 else view,width='stretch',hide_index=True,
                             column_order=['Symbol','Side','Candle close (IST)','Open','High','Low','Close','Volume',
                                           'Score','Qualified on candle','Qualified closes (IST)','Detected (IST)','Detection delay (s)',*CHECKS,'Setup bias','Volume source','Candle start (IST)',
                                           'Exit ST (7,2)','Reversal'],
                             column_config={'Symbol':st.column_config.TextColumn(pinned=True),
                                            'Close':st.column_config.NumberColumn(format='%.2f')})
            st.download_button('Download scanner CSV (both sides)',df.to_csv(index=False),'intraday_scanner.csv')
        else:st.info('Waiting for the first full-universe scan. In dashboard mode, Refresh Now can also load a historical preview.')
    if tab == 1:
        st.subheader('Eligible CE / PE signals')
        history=live.signal_history
        st.caption('Qualifying times are candle closes in IST, saved separately for CE and PE. Detection delay measures when the scanner scored the candle, not when it reached your browser.')
        card_side=st.radio('Signal cards side',('CE · Bullish','PE · Bearish'),horizontal=True,key='signal_cards_side_52')
        eligible=sorted((x for x in r if x['side']==card_side[:2] and x['eligible']),key=lambda x:(-x['score'],x['symbol']))
        if not eligible:st.info('No eligible signals for this side in the displayed candle snapshot.')
        for i in range(0,len(eligible),3):
            cols=st.columns(3)
            for col,x in zip(cols,eligible[i:i+3]):
                with col:
                    label='Bullish' if x['side']=='CE' else 'Bearish'
                    border=BIAS_COLORS[label][0]
                    st.markdown(f'''<div style="background:#10223a;border:2px solid {border};border-radius:17px;padding:18px;color:#eaf4ff">
                    <h3 style="color:{border}!important">{html.escape(x['symbol'])} · {x['side']} · {label}</h3>
                    <h2 style="color:#5de7ff!important">{x['score']}/8</h2><p>Underlying close ₹{x['close']:,.2f}</p>
                    <small>{candle_range(x['candle'])}</small>
                    <p>Qualified closes (IST): {html.escape(times_for(history,x) or '—')}</p>
                    <small>{html.escape(('Detected in '+str(x['detect_seconds'])+' s · '+('Within 60 s' if x['detect_seconds']<=60 else 'Over 60 s')) if x.get('detect_seconds') is not None else 'Detection time unavailable')}</small></div>''',unsafe_allow_html=True)
                    with st.expander('Condition checklist'):
                        st.caption('Qualified closes (IST): '+(times_for(history,x) or '—'))
                        for name, value in x['tests'].items():
                            st.write(('✅' if value is True else '❌' if value is False else '🟡')+' '+name)
        saved=[{'Session (IST)':history.get('date'),'Symbol':key.rsplit(' ',1)[0],'Side':key.rsplit(' ',1)[1],
                'Qualified closes (IST)':', '.join(values),'Count':len(values)}
               for key,values in sorted(history.get('times',{}).items()) if key.endswith(' '+card_side[:2])]
        with st.expander('Saved session signals · includes setups no longer eligible',expanded=not eligible):
            if saved:
                df=pd.DataFrame(saved)
                st.dataframe(df,width='stretch',hide_index=True)
                st.download_button('Export qualifying times CSV',df.to_csv(index=False),'intraday_signal_times.csv')
            else:
                st.info('No qualifying times saved for this side in the current scan session.')
    if tab == 2:
        st.subheader('Candles + EMA + VWAP')
        if live.candles:
            symbol=st.selectbox('Instrument',list(live.candles),key='chart_symbol')
            d=live.candles[symbol].tail(90)
            st.caption(f'Latest candle for {symbol} · {candle_range(d.index[-1])}')
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
                st.plotly_chart(fig,width='stretch')
            except ImportError:st.line_chart(d[['Close','ema9','ema21','vwap']])
            st.caption(LEGEND + ' · Candles compare Close to Open. Supertrend cells show the indicator’s own direction.')
            recent=d.tail(20).iloc[::-1].copy()
            recent.insert(0,'Candle close (IST)',[candle_close(i) for i in recent.index])
            st.dataframe(candle_table(recent),width='stretch',hide_index=True)
        else:st.info('Scan first to view candles.')
    if tab == 3:
        st.subheader('Automatic paper journal')
        st.caption('No daily entry count cap. The 3-open-position and 35% total premium exposure limits still apply.')
        st.caption('Entry at Dhan ask, exits at observed Dhan bid. Prices are snapshots. Missed SL/target between refreshes cannot be reconstructed.')
        for note in live.notes[:15]:
            if 'paper' in note.lower() or any(x in note for x in ('SKIP','lookups','entries','candle expired','outside')):st.text(note)
        if j.empty:st.info('No paper trades yet. The diagnostics above show what blocked the latest cycle.')
        else:
            st.markdown('**Open trades**');st.dataframe(j[j.status=='OPEN'],width='stretch',hide_index=True)
            st.markdown('**Closed trades**');st.dataframe(j[j.status=='CLOSED'],width='stretch',hide_index=True)
            closed=j[j.status=='CLOSED'].copy()
            if len(closed):
                p=pd.to_numeric(closed.pnl,errors='coerce').dropna()
                a,b,c=st.columns(3);a.metric('Realized net P&L ₹',f'{p.sum():,.2f}');b.metric('Win rate',f'{(p>0).mean()*100:.1f}%' if len(p) else '—');c.metric('Closed trades',len(closed))
            st.subheader('Paper performance by symbol and side')
            st.caption('Date filter uses the entry session in IST. Realized P&L counts only closed trades and includes estimated charges. Open trades are counted separately.')
            days=sorted(pd.to_datetime(j.opened_at,utc=True,errors='coerce',format='mixed').dt.tz_convert(IST).dt.strftime('%Y-%m-%d').dropna().unique(),reverse=True)
            period=st.selectbox('Entry session',['All sessions',*days],key='paper_summary_session')
            summary=performance_by_symbol(j,None if period=='All sessions' else period)
            st.dataframe(summary,width='stretch',hide_index=True)
            st.download_button('Download paper performance CSV',summary.to_csv(index=False),'paper_performance_by_symbol.csv')
            st.download_button('Download paper journal CSV',j.to_csv(index=False),'intraday_paper_trades.csv')
        if secrets.get('GITHUB_TOKEN') and live.gist and not secrets.get('INTRADAY_GIST_ID'):
            st.warning(f'Add this journal ID to Streamlit Secrets to reconnect after restart: INTRADAY_GIST_ID = "{live.gist}"')
        if not secrets.get('GITHUB_TOKEN'):st.warning('Persistent journal is NOT configured. Local Streamlit Cloud storage is temporary. Add GITHUB_TOKEN and INTRADAY_GIST_ID.')
    if tab == 4:
        st.subheader('Conditions — green passed / red failed / yellow unverified')
        if r:
            symbol=st.selectbox('Choose instrument',sorted(set(x['symbol'] for x in r)),key='cond_symbol')
            for x in [v for v in r if v['symbol']==symbol]:
                st.markdown(f"**{x['side']} — {x['score']}/8 — {'ELIGIBLE' if x['eligible'] else 'WATCH / SKIP'}**")
                for k,v in x['tests'].items():
                    st.markdown(('🟢' if v is True else '🔴' if v is False else '🟡')+' '+k+(' — not verified' if v is None else ''))
        st.caption('Combined entry requires ≥ threshold, ST(10,3), EMA9/21, VWAP, volume 1.5×, and CPR/PDH trigger. Exit: premium SL, configured R target, ST(7,2) reversal, opposing eligible signal or 15:20 IST. These are configurable research rules, not a verified profitable strategy.')

    if tab == 5:
        st.subheader('Intraday trade decision')
        if not r:
            st.info('Waiting for the first published scan.')
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
                journal_ok=not live.error and (not worker_mode or bool(live.status))
                cfg={**params,**live.policy}
                policy=dict(capital=cfg['capital'],cap=cfg['cap'],risk=cfg['risk'],stop=cfg['stop']/100,target_r=cfg['target'],slip=.005,
                    delta_lo=.4,delta_hi=.75,spread=5,min_days=2,exposure=35,max_open=3,
                    has_connection=bool(token and client),journal_ok=bool(journal_ok),
                    start=(9,45),end=(15,10),window='09:45–15:10 IST (regular weekdays)',worker_mode=worker_mode)
                checks=[('Fresh 5-minute signal',bool(table_live and fresh_candle(match['candle'],datetime.now(IST))),
                         'Only the latest completed 5-minute candle can be considered.')]
                def inspect(p):
                    api=DhanAPI(token,client)
                    notes=[]
                    d=live.candles.get(symbol)
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
                if worker_mode: st.caption('This uses the worker’s published paper settings.')
                st.caption('The extra 15-minute and fee-adjusted checks in this panel are advisory and do not change the paper engine.')
                show_panel(idea,policy,j,inspect,key='preflight_intraday',base_checks=checks,
                           critical_context=['15-minute EMA trend'],
                           exit_rules=['Premium stop or target at an observed Dhan bid.',
                                       'Supertrend (7,2) reversal or opposing eligible signal.',
                                       'End-of-day exit checks start at 15:20 IST; a valid bid is needed.'])

    if tab == 7:
        def quote_stock(symbol, side):
            api=DhanAPI(token,client)
            with api.session:
                return choose_option(api,lookup[symbol],side,capital,max_premium=cap)
        render_breakouts(r,lookup,CHECKS,now,visible_slot,regular_day(now,settings),quote_stock)

PAGES=(
    '1 · Market scanner','2 · Eligible signal cards','3 · Trade analysis',
    '4 · Paper trading','5 · Conditions','6 · Trade Decision','7 · Call / Put Backtest','8 · ⚡ Breakout (15m)',
)


@st.fragment(run_every=3)
def live_view(page):
    """Use one snapshot and one render pass for the visible page."""
    if not is_authenticated():
        st.rerun()
    data=dashboard()
    if page!=6:
        render_dashboard(page,data)


page=st.radio('Dashboard view',PAGES,horizontal=True,key='intraday_view_52',label_visibility='collapsed')
index=PAGES.index(page)
live_view(index)
if index==6:
    st.caption('Backtests run on demand and are separate from the live five-minute cycle.')
    render_backtest(all_u,token,client,DhanAPI,'5 min')
