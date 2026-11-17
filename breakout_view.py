"""Read-only opening-range breakout view for the existing Intraday scanner."""
import pandas as pd
import streamlit as st

from breakout import IST


def render_breakouts(rows, lookup, checks, now, visible_slot, regular_session, quote_stock):
    now = pd.Timestamp(now).tz_convert(IST)
    st.subheader('⚡ 5-minute crosses of the opening 15-minute candle body')
    st.caption('The 09:15–09:30 IST open and close set two fixed levels. An upside cross closes above the higher level after the previous 5-minute close was at or below it. A downside cross closes below the lower level after the previous close was at or above it. Wicks and repeated closes beyond the same level do not count.')
    st.caption('The breakout is shown even when the original strategy fails. ✓ Passed · ✗ Not met · — Unverified. The original checklist is frozen at each 5-minute breakout close. This tab places no orders and does not trigger paper entries.')
    if not rows:
        st.info('Waiting for the Intraday scan. Use Refresh Now to load a snapshot.')
        return

    latest_bar = visible_slot if visible_slot is not None else max(pd.Timestamp(x['candle']) for x in rows)
    latest_bar = pd.Timestamp(latest_bar).tz_convert(IST)
    snapshot_end = latest_bar + pd.Timedelta(minutes=5)
    latest_close = snapshot_end.floor('5min')
    session = latest_bar.date().isoformat()
    market_end = now.normalize() + pd.Timedelta(hours=15, minutes=30)
    latest_expected = min(now.floor('5min'), market_end)
    if latest_bar.date() != now.date() or not regular_session or now > market_end + pd.Timedelta(minutes=5):
        st.info(f'Showing the last scanned session ({latest_bar:%d %b %Y} IST), not live signals.')
    elif latest_expected > latest_close:
        st.info(f'The {latest_expected:%H:%M} IST candle is not published yet. Showing the snapshot through {snapshot_end:%H:%M} IST.')

    stocks = {sym for sym, item in lookup.items() if item.get('instrument') != 'INDEX'}
    packets = [(x['symbol'], x.get('opening_breakout_5m')) for x in rows if x['symbol'] in stocks and x['side']=='CE']
    packets = [(sym, p) for sym, p in packets if p and p['session'] == session]
    if latest_close.time() < pd.Timestamp('09:35').time():
        st.info('The first possible cross confirmation is 09:35 IST.')
        return
    openings = sum(p['opening_ready'] for _, p in packets)
    current = sum(bool(p['last_5m_close']) and pd.Timestamp(p['last_5m_close']) == latest_close for _, p in packets)
    st.caption(f'{latest_bar:%d %b %Y} · 5m close through {latest_close:%H:%M} IST · Opening reference ready {openings}/{len(stocks)} F&O stocks · Valid data for this 5m cross check {current}/{len(stocks)}. Missing candles are not backfilled with old prices.')

    period = st.radio('Breakout window', ('Scanned session', 'Latest completed 5m candle'), horizontal=True, key='opening5_window')
    direction = st.radio('Direction', ('Both', 'Up · CE', 'Down · PE'), horizontal=True, key='opening5_direction')
    events = sorted(((sym,e) for sym,p in packets for e in p['events']
                     if (direction == 'Both' or e['side'] == direction[-2:])
                     and (period == 'Scanned session' or pd.Timestamp(e['end']) == latest_close)),
                    key=lambda pair:(-pd.Timestamp(pair[1]['end']).value, pair[0]))
    if not events:
        st.info('No confirmed breakouts match this view in the available candle data.')
        return

    table = []
    for sym, e in events:
        table.append({
            'Breakout': '↑ BREAKOUT UP' if e['side']=='CE' else '↓ BREAKOUT DOWN',
            'Symbol': sym, 'Side': e['side'],
            'Candle start (IST)': pd.Timestamp(e['start']).strftime('%H:%M'),
            'Candle close (IST)': pd.Timestamp(e['end']).strftime('%H:%M'),
            '5m open ₹': e['open'], '5m high ₹': e['high'], '5m low ₹': e['low'],
            '5m close ₹': e['close'], '5m volume': e['volume'], 'Level crossed ₹': e['level'], 'Previous 5m close ₹': e['previous_close'],
            'Beyond level ₹': e['move_points'], 'Beyond level %': e['move_pct'],
            'First 15m open ₹': e['reference_open'], 'First 5m close ₹': e['reference_close'],
            'Upper reference ₹': e['upper'], 'Lower reference ₹': e['lower'],
            'Existing score': f"{e['score']}/{e['max_score']}",
            'Existing eligible': 'Yes' if e['eligible'] else 'No',
            **{k: '✓' if e['tests'].get(k) is True else '✗' if e['tests'].get(k) is False else '—' for k in checks},
        })
    df = pd.DataFrame(table)
    c1,c2 = st.columns(2)
    c1.metric('↑ Upside breakouts', int(df.Side.eq('CE').sum()))
    c2.metric('↓ Downside breakouts', int(df.Side.eq('PE').sum()))
    marks = pd.DataFrame('', index=df.index, columns=df.columns)
    up = df.Side.eq('CE')
    marks.loc[up, ['Breakout','Symbol','5m close ₹']] = 'color:#a5f3ca;background-color:#123e35;font-weight:bold'
    marks.loc[~up, ['Breakout','Symbol','5m close ₹']] = 'color:#ffb3ba;background-color:#51232d;font-weight:bold'
    for col in checks:
        marks.loc[df[col].eq('✓'), col] = 'color:#a5f3ca;background-color:#123e35'
        marks.loc[df[col].eq('✗'), col] = 'color:#ffb3ba;background-color:#51232d'
    st.dataframe(df.style.apply(lambda _: marks, axis=None), width='stretch', hide_index=True,
                 column_config={'Breakout': st.column_config.TextColumn(pinned=True),
                                'Symbol': st.column_config.TextColumn(pinned=True),
                                **{col: st.column_config.NumberColumn(format='%.2f') for col in df if '₹' in col or '%' in col},
                                '5m volume': st.column_config.NumberColumn(format='%.0f')})
    st.download_button('Download breakout CSV', df.to_csv(index=False), f'opening_breakouts_{session}.csv', key='opening5_csv')

    labels = [f"{sym} · {'UP' if e['side']=='CE' else 'DOWN'} · {pd.Timestamp(e['end']):%H:%M} IST" for sym,e in events]
    selection = dict(zip(labels, events))
    chosen = st.selectbox('Inspect a breakout', labels, key='opening5_selected')
    sym, event = selection[chosen]
    marker = '🟢 ↑ BREAKOUT UP' if event['side']=='CE' else '🔴 ↓ BREAKOUT DOWN'
    st.markdown(f"**{marker} · {sym} · {event['side']} · ₹{event['close']:,.2f} at {pd.Timestamp(event['end']):%H:%M} IST**")
    if regular_session and latest_bar.date() == now.date() and (9,15) <= (now.hour,now.minute) < (15,30):
        if st.button(f"Check current Dhan {event['side']} quote for {sym}", key='opening5_quote'):
            try:
                quote, reason = quote_stock(sym, event['side'])
                st.session_state['opening5_quote_result'] = (sym,event['end'],event['side'],pd.Timestamp.now(tz=IST),quote,reason)
            except Exception:
                st.session_state['opening5_quote_result'] = (sym,event['end'],event['side'],now,None,'Dhan option quote unavailable. Try again or check your connection.')
        saved = st.session_state.get('opening5_quote_result')
        if saved and saved[:3] == (sym,event['end'],event['side']):
            _,_,_,quote_time,quote,reason = saved
            if quote:
                st.info(f"{quote['contract']} · Bid ₹{quote['bid']:,.2f} · Ask ₹{quote['premium']:,.2f} · Lot {quote['lot']} · Checked {quote_time:%H:%M:%S} IST. This is a quote snapshot, not the premium at breakout time.")
            else:
                st.info(reason or 'No eligible Dhan contract returned.')
