"""Report realized paper results by underlying and side, without marking open P&L."""
import pandas as pd


def performance_by_symbol(journal, entry_day=None):
    df=journal[['symbol','side','status','opened_at','pnl']].copy()
    if entry_day is not None:
        date=pd.to_datetime(df.opened_at,errors='coerce',utc=True,format='mixed').dt.tz_convert('Asia/Kolkata')
        df=df[date.dt.strftime('%Y-%m-%d').eq(entry_day)].copy()
    df['Open']=df.status.eq('OPEN')
    df['Closed']=df.status.eq('CLOSED')
    df['Realized']=pd.to_numeric(df.pnl,errors='coerce').where(df['Closed'])
    df['Wins']=df.Realized.gt(0)
    report=df.groupby(['symbol','side'],sort=True).agg(
        Entries=('status','size'),Open=('Open','sum'),Closed=('Closed','sum'),
        Wins=('Wins','sum'),Scored=('Realized','count'),Net=('Realized',lambda s:s.sum(min_count=1)))
    report['Win rate %']=(report.Wins.div(report.Scored.where(report.Scored.gt(0)))*100).round(1)
    report=report.drop(columns='Scored').rename(columns={'Net':'Realized net P&L ₹'})
    return report.reset_index().rename(columns={'symbol':'Symbol','side':'Side'}).sort_values(
        ['Realized net P&L ₹','Symbol','Side'],ascending=[False,True,True],ignore_index=True)
