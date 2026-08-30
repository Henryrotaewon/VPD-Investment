"""VWPI5 Shadow 3.0 - expanded validation over up to 200 Upbit KRW markets.
Primary hypothesis is frozen before the run:
VPDReplay >= 65 AND MaxPriceReturn <= 10% AND VWPI5 >= 20.
VPD v1.5 production score is NOT modified.
"""
from pathlib import Path
import json, time, requests
import pandas as pd
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
OUT_JSON=ROOT/'data'/'vwpi5_validation_200_latest.json'
OUT_CSV=ROOT/'data'/'vwpi5_validation_200.csv'
UA={'User-Agent':'VPD-VWPI5-Validation/3.0'}

def get(url,params=None,retry=4):
    for n in range(retry):
        try:
            r=requests.get(url,params=params,headers=UA,timeout=15)
            if r.status_code==200:return r.json()
            if r.status_code==429: time.sleep(1+n)
        except Exception: time.sleep(.5+n*.3)
    return None

def markets():
    x=get('https://api.upbit.com/v1/market/all',{'is_details':'false'}) or []
    return sorted([i['market'] for i in x if i['market'].startswith('KRW-')])[:200]

def candles(m,count=80):
    x=get('https://api.upbit.com/v1/candles/days',{'market':m,'count':count})
    if not x:return None
    x.reverse(); return pd.DataFrame(x)

def ratio(a,b):
    try:return np.nan if pd.isna(a) or pd.isna(b) or float(b)==0 else float(a)/float(b)
    except:return np.nan

def rsi(c,p=14):
    c=pd.Series(c).astype(float); d=c.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    ag=g.ewm(alpha=1/p,adjust=False).mean(); al=l.ewm(alpha=1/p,adjust=False).mean(); rs=ag/al.replace(0,np.nan)
    return 100-100/(1+rs)

def will(df,p=14):
    h=df.high_price.astype(float).rolling(p).max(); l=df.low_price.astype(float).rolling(p).min(); c=df.trade_price.astype(float)
    return -100*(h-c)/(h-l).replace(0,np.nan)

def score(v3,tv,ia,r,w,maxret,spike):
    s=0
    if not pd.isna(v3): s += 25 if v3>=3 else 21 if v3>=2 else 17 if v3>=1.5 else 12 if v3>=1.2 else 7 if v3>=1 else 0
    if not pd.isna(tv): s += 20 if tv>=3 else 17 if tv>=2 else 13 if tv>=1.2 else 9 if tv>=.8 else 5 if tv>=.4 else 0
    if not pd.isna(ia): s += 15 if ia>=4 else 12 if ia>=2.5 else 9 if ia>=1.5 else 5 if ia>=1 else 0
    if not pd.isna(r): s += 15 if 45<=r<=65 else 12 if 35<=r<45 else 10 if 65<r<=72 else 8 if 30<=r<35 else 5 if 72<r<=78 else 0
    if not pd.isna(w): s += 10 if -80<=w<=-40 else 8 if -40<w<=-20 else 7 if -90<=w<-80 else 4 if -20<w<=-10 else 0
    if not pd.isna(maxret):
        a=abs(maxret); s += 15 if a<=2 else 13 if a<=4 else 10 if a<=6 else 6 if a<=10 else 2 if a<=15 else 0
    if spike:s-=15
    return max(0,min(100,s))

def vwpi5(f):
    lo=up=0.0
    for _,x in f.iterrows():
        o,h,l,c=map(float,[x.opening_price,x.high_price,x.low_price,x.trade_price]); rng=h-l; val=max(float(x.candle_acc_trade_price),0)
        if rng<=0:continue
        lr=max(min(o,c)-l,0)/rng; ur=max(h-max(o,c),0)/rng
        lo+=lr*val; up+=ur*val
    return 0.0 if lo+up==0 else (lo-up)/(lo+up)*100

def fwd(df,i):
    base=float(df.iloc[i].trade_price); z={}
    for n in (1,3,5): z[f'D{n}%']=((float(df.iloc[i+n].trade_price)/base-1)*100) if i+n<len(df) else None
    q=df.iloc[i+1:min(i+6,len(df))]
    z['MFE5%']=(q.high_price.astype(float).max()/base-1)*100 if len(q) else None
    z['MAE5%']=(q.low_price.astype(float).min()/base-1)*100 if len(q) else None
    return z

def perf(g):
    o={'N':int(len(g))}
    for c in ['D1%','D3%','D5%','MFE5%','MAE5%']:
        z=pd.to_numeric(g[c],errors='coerce').dropna(); o['Avg'+c]=round(float(z.mean()),2) if len(z) else None
    z=pd.to_numeric(g['D3%'],errors='coerce').dropna(); o['D3WinRate%']=round(float((z>0).mean()*100),1) if len(z) else None
    return o

def main():
    rows=[]; ms=markets(); ok=0
    for k,m in enumerate(ms,1):
        df=candles(m,80)
        if df is None or len(df)<35: continue
        df=df.iloc[:-1].copy().reset_index(drop=True); df['RSI']=rsi(df.trade_price); df['W']=will(df)
        vol=df.candle_acc_trade_volume.astype(float); val=df.candle_acc_trade_price.astype(float)
        start=max(20,len(df)-35)
        for i in range(start,len(df)):
            pc=float(df.iloc[i-1].trade_price); high=float(df.iloc[i].high_price); maxret=(high/pc-1)*100 if pc else np.nan
            v3=ratio(vol.iloc[i-2:i+1].mean(),vol.iloc[i-10:i].mean()); tv=ratio(val.iloc[i],val.iloc[i-10:i].mean()); ia=ratio(val.iloc[i],val.iloc[i-3:i].mean())
            pv=val.iloc[i-10:i]; med=pv.median(); mx=pv.max(); spike=bool(med>0 and mx>0 and mx/med>=5 and val.iloc[i]/mx<=.25)
            vp=score(v3,tv,ia,df.iloc[i].RSI,df.iloc[i].W,maxret,spike); vw=vwpi5(df.iloc[i-5:i])
            rows.append({'market':m,'coin':m[4:],'date':str(df.iloc[i].candle_date_time_kst)[:10],'VPDReplay':round(vp,1),'MaxPriceReturn%':round(maxret,2),'VWPI5':round(vw,2),**fwd(df,i)})
        ok+=1; time.sleep(.08)
        if k%25==0: print(f'progress {k}/{len(ms)}, valid {ok}, rows {len(rows)}')
    rdf=pd.DataFrame(rows); rdf.to_csv(OUT_CSV,index=False,encoding='utf-8-sig')
    base=perf(rdf); key=rdf[(rdf.VPDReplay>=65)&(rdf['MaxPriceReturn%']<=10)&(rdf.VWPI5>=20)]
    controls={}
    for th in [0,10,20,30,40]: controls[f'VWPI{th}+']=perf(rdf[(rdf.VPDReplay>=65)&(rdf['MaxPriceReturn%']<=10)&(rdf.VWPI5>=th)])
    by_coin=[]
    for coin,g in key.groupby('coin'): by_coin.append({'coin':coin,**perf(g)})
    by_coin=sorted(by_coin,key=lambda x:x['N'],reverse=True)
    doc={'name':'VWPI5 Shadow 3.0 200-Market Validation','markets_requested':200,'markets_valid':ok,'rows':len(rdf),'primary_rule':'VPDReplay>=65 AND MaxPriceReturn<=10 AND VWPI5>=20','base':base,'primary':perf(key),'controls':controls,'primary_by_coin':by_coin,'limitations':['Completed-day replay, not exact historical intraday scan snapshots.','First 200 KRW markets sorted by market code; no performance-based market selection.','VWPI threshold 20 and Max 10 were fixed before this expanded run.']}
    OUT_JSON.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(doc,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
