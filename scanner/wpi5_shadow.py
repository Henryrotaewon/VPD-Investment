"""WPI5 Shadow: 5-day wick pressure index + 30-day validation for current VPD TOP10.
Does NOT modify VPD v1.5 score.
Source of truth: Upbit KRW daily OHLCV only.
"""
import json, time
from pathlib import Path
import requests
import pandas as pd
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
LATEST=ROOT/'data'/'vpd_latest.json'
OUT_JSON=ROOT/'data'/'wpi5_shadow_latest.json'
OUT_CSV=ROOT/'data'/'wpi5_validation_30d.csv'
UA={'User-Agent':'VPD-WPI5-Shadow/1.0'}

def candles(market,count=45):
    r=requests.get('https://api.upbit.com/v1/candles/days',params={'market':market,'count':count},headers=UA,timeout=15)
    r.raise_for_status(); a=r.json(); a.reverse(); return pd.DataFrame(a)

def wick_metrics(frame):
    # frame must be completed candles only, oldest -> newest
    lows=[]; ups=[]
    for _,r in frame.iterrows():
        o=float(r.opening_price); h=float(r.high_price); l=float(r.low_price); c=float(r.trade_price)
        rng=max(h-l,0.0)
        if rng<=0: lo=up=0.0
        else:
            lo=max(min(o,c)-l,0.0)/rng
            up=max(h-max(o,c),0.0)/rng
        lows.append(lo); ups.append(up)
    sl=float(sum(lows)); su=float(sum(ups)); den=sl+su
    wpi=0.0 if den==0 else (sl-su)/den*100.0
    ld=sum(1 for lo,up in zip(lows,ups) if lo>up)
    ud=sum(1 for lo,up in zip(lows,ups) if up>lo)
    if wpi>=20: status='BULLISH_WICK'
    elif wpi<=-20: status='BEARISH_WICK'
    else: status='NEUTRAL'
    return {'WPI5':round(wpi,2),'LowerDominantDays5':ld,'UpperDominantDays5':ud,
            'LowerWickTotal5':round(sl,4),'UpperWickTotal5':round(su,4),'WPI5Status':status}

def forward_stats(df,i):
    base=float(df.iloc[i].trade_price); out={}
    for n in (1,3,5):
        if i+n<len(df): out[f'D{n}%']=round((float(df.iloc[i+n].trade_price)/base-1)*100,2)
        else: out[f'D{n}%']=None
    end=min(i+6,len(df)); future=df.iloc[i+1:end]
    if len(future):
        out['MFE5%']=round((future.high_price.astype(float).max()/base-1)*100,2)
        out['MAE5%']=round((future.low_price.astype(float).min()/base-1)*100,2)
    else: out['MFE5%']=out['MAE5%']=None
    return out

def main():
    latest=json.loads(LATEST.read_text(encoding='utf-8')); top=latest.get('top10',[])
    today_rows=[]; val=[]
    for x in top:
        coin=x['coin']; market=x['market']; df=candles(market,45)
        # Upbit newest daily candle is today's partial candle. WPI5 uses five completed days before it.
        completed=df.iloc[:-1].copy()
        wm=wick_metrics(completed.tail(5)) if len(completed)>=5 else {}
        today_rows.append({'Rank':x['Rank'],'coin':coin,'VPD':x['VPD'],'MaxPriceReturn%':x.get('MaxPriceReturn%'),**wm})
        # roughly last 30 completed daily observations; each historical WPI uses prior 5 completed candles only.
        start=max(5,len(completed)-30)
        for i in range(start,len(completed)):
            hist=wick_metrics(completed.iloc[i-5:i])
            row={'coin':coin,'date':str(completed.iloc[i].candle_date_time_kst)[:10],**hist,**forward_stats(completed,i)}
            val.append(row)
        time.sleep(0.08)
    vdf=pd.DataFrame(val)
    summary=[]
    if len(vdf):
        for status,g in vdf.groupby('WPI5Status'):
            s={'group':status,'N':len(g),'AvgWPI5':round(g.WPI5.mean(),2)}
            for col in ('D1%','D3%','D5%','MFE5%','MAE5%'):
                z=pd.to_numeric(g[col],errors='coerce').dropna(); s['Avg'+col]=round(z.mean(),2) if len(z) else None
            z=pd.to_numeric(g['D3%'],errors='coerce').dropna(); s['D3WinRate%']=round((z>0).mean()*100,1) if len(z) else None
            summary.append(s)
        vdf.to_csv(OUT_CSV,index=False,encoding='utf-8-sig')
    doc={'name':'WPI5 Shadow v1.0','source':'Upbit KRW daily OHLCV','note':'Shadow only; VPD v1.5 score unchanged',
         'definition':'WPI5=(sum normalized lower wicks - sum normalized upper wicks)/(sum both)*100 over prior 5 completed candles',
         'today_top10':today_rows,'validation_30d_summary':summary,'validation_rows':len(vdf)}
    OUT_JSON.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(doc,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
