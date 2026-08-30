"""Point-in-time VPD v1.5-style replay.

Purpose: reconstruct what the scanner could have known at a historical KST timestamp,
without using candles after that timestamp. This is validation-only and does not send Telegram.

Targets default to 2026-08-29 07:20, 17:50 and 2026-08-30 07:20 KST.
Runs the whole Upbit KRW universe so historical Rank/TOP10 can be evaluated.

Important limitation: Upbit historical minute API retention/availability determines whether
old intraday candles can be reconstructed. The script records failures explicitly rather than
silently substituting end-of-day data.
"""
import json, time, requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

KST=ZoneInfo("Asia/Seoul")
BASE="https://api.upbit.com/v1"
OUT=Path("data/point_in_time_replay_zkc.json")
TARGETS=["2026-08-29 07:20:00","2026-08-29 17:50:00","2026-08-30 07:20:00"]
FOCUS={"ZKC","KNC","SKR"}
s=requests.Session(); s.headers.update({"Accept":"application/json","User-Agent":"MAGI-PointInTime-Replay/1.0"})

def get(path,params,retry=4):
    for a in range(retry):
        try:
            r=s.get(BASE+path,params=params,timeout=15)
            if r.status_code==200:return r.json()
            if r.status_code==429:time.sleep(1+a);continue
        except Exception:pass
        time.sleep(.5)
    return None

def rsi(close,p=14):
    x=pd.Series(close,dtype=float); d=x.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    ag=g.ewm(alpha=1/p,adjust=False).mean(); al=l.ewm(alpha=1/p,adjust=False).mean(); rs=ag/al.replace(0,np.nan)
    return 100-100/(1+rs)

def will(df,p=14):
    hi=df.high_price.rolling(p).max(); lo=df.low_price.rolling(p).min(); den=(hi-lo).replace(0,np.nan)
    return -100*(hi-df.trade_price)/den

def ratio(a,b): return np.nan if pd.isna(a) or pd.isna(b) or b==0 else float(a)/float(b)

def score(v3,tv,ia,r,w,maxret,spike):
    z=0
    if not pd.isna(v3): z += 25 if v3>=3 else 21 if v3>=2 else 17 if v3>=1.5 else 12 if v3>=1.2 else 7 if v3>=1 else 0
    if not pd.isna(tv): z += 20 if tv>=3 else 17 if tv>=2 else 13 if tv>=1.2 else 9 if tv>=.8 else 5 if tv>=.4 else 0
    if not pd.isna(ia): z += 15 if ia>=4 else 12 if ia>=2.5 else 9 if ia>=1.5 else 5 if ia>=1 else 0
    if not pd.isna(r): z += 15 if 45<=r<=65 else 12 if 35<=r<45 else 10 if 65<r<=72 else 8 if 30<=r<35 else 5 if 72<r<=78 else 0
    if not pd.isna(w): z += 10 if -80<=w<=-40 else 8 if -40<w<=-20 else 7 if -90<=w<-80 else 4 if -20<w<=-10 else 0
    if not pd.isna(maxret):
        a=abs(maxret); z += 15 if a<=2 else 13 if a<=4 else 10 if a<=6 else 6 if a<=10 else 2 if a<=15 else 0
    if spike:z-=15
    return round(max(0,min(100,z)),1)

def momentum(rn,rp,wn,wp,h,v3):
    p=0
    if not pd.isna(rn) and not pd.isna(rp): p += 2 if rn-rp>=5 else 1 if rn-rp>0 else 0
    if not pd.isna(wn) and not pd.isna(wp): p += 2 if wn-wp>=15 else 1 if wn-wp>0 else 0
    if not pd.isna(h): p += 1 if 0<h<=3 else 2 if 3<h<=6 else 0
    if not pd.isna(v3): p += 2 if v3>=2 else 1 if v3>=1.2 else 0
    return "↑↑" if p>=6 else "↑" if p>=3 else "→" if p>=1 else "↓"

def utc_iso(dt): return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

def minute_candles(market,to_kst,count=200):
    d=get("/candles/minutes/1",{"market":market,"to":utc_iso(to_kst),"count":count})
    return None if not d else pd.DataFrame(d).sort_values("candle_date_time_kst").reset_index(drop=True)

def daily_before(market,day_start_kst,count=34):
    # Upbit 'to' is exclusive-ish; request just before current Upbit day starts.
    d=get("/candles/days",{"market":market,"to":utc_iso(day_start_kst),"count":count})
    return None if not d else pd.DataFrame(d).sort_values("candle_date_time_kst").reset_index(drop=True)

def analyse(market,coin,t):
    # Upbit daily trading day starts 09:00 KST. At 07:20/17:50 choose the active 09:00 boundary.
    boundary=t.replace(hour=9,minute=0,second=0,microsecond=0)
    if t<boundary: boundary-=timedelta(days=1)
    hist=daily_before(market,boundary,34)
    mins=minute_candles(market,t,200)
    if hist is None or len(hist)<19 or mins is None or len(mins)==0:return None,"NO_HISTORICAL_INTRADAY_DATA"
    # Need the whole current trading-day accumulation. Fetch backward in 200-minute pages.
    frames=[mins]; cursor=pd.Timestamp(mins.iloc[0].candle_date_time_kst).tz_localize(KST)
    while cursor>boundary and sum(len(x) for x in frames)<1500:
        older=minute_candles(market,cursor.to_pydatetime(),200)
        if older is None or len(older)==0:break
        older=older[older.candle_date_time_kst < frames[-1].iloc[0].candle_date_time_kst]
        if len(older)==0:break
        frames.append(older); cursor=pd.Timestamp(older.iloc[0].candle_date_time_kst).tz_localize(KST); time.sleep(.03)
    m=pd.concat(frames).drop_duplicates("candle_date_time_kst").sort_values("candle_date_time_kst")
    m=m[pd.to_datetime(m.candle_date_time_kst)>=pd.Timestamp(boundary).tz_localize(None)]
    if len(m)==0:return None,"CURRENT_DAY_MINUTES_MISSING"
    cur={"candle_date_time_kst":boundary.strftime("%Y-%m-%dT%H:%M:%S"),"opening_price":float(m.iloc[0].opening_price),"high_price":float(m.high_price.max()),"low_price":float(m.low_price.min()),"trade_price":float(m.iloc[-1].trade_price),"candle_acc_trade_volume":float(m.candle_acc_trade_volume.sum()),"candle_acc_trade_price":float(m.candle_acc_trade_price.sum())}
    df=pd.concat([hist,pd.DataFrame([cur])],ignore_index=True); df["RSI14"]=rsi(df.trade_price); df["Williams"]=will(df)
    latest=df.iloc[-1]; prev=df.iloc[-2]; price=float(latest.trade_price); pc=float(prev.trade_price); dr=(price/pc-1)*100; mx=(float(latest.high_price)/pc-1)*100
    vol=df.candle_acc_trade_volume.astype(float); val=df.candle_acc_trade_price.astype(float)
    v3=ratio(vol.iloc[-3:].mean(),vol.iloc[-11:-1].mean()); tv=ratio(val.iloc[-1],val.iloc[-11:-1].mean()); ia=ratio(val.iloc[-1],val.iloc[-4:-1].mean())
    pv=val.iloc[-11:-1]; med=pv.median(); ma=pv.max(); spike=bool(med>0 and ma>0 and ma/med>=5 and val.iloc[-1]/ma<=.25)
    rn=float(df.RSI14.iloc[-1]); rp=float(df.RSI14.iloc[-2]); wn=float(df.Williams.iloc[-1]); wp=float(df.Williams.iloc[-2])
    # Historical 1H from minute candles, using last completed-ish 60m price comparison.
    h=np.nan
    if len(m)>=61:
        p0=float(m.iloc[-1].trade_price); p1=float(m.iloc[-61].trade_price); h=(p0/p1-1)*100 if p1 else np.nan
    v=score(v3,tv,ia,rn,wn,mx,spike); mom=momentum(rn,rp,wn,wp,h,v3)
    trig="A" if v>=85 and mom in ("↑","↑↑") else "B" if 75<=v<85 and mom=="↑↑" and abs(mx)<=6 else "-"
    return {"coin":coin,"market":market,"price":price,"VPD":v,"momentum":mom,"trigger":trig,"1D%":round(dr,2),"MaxPriceReturn%":round(mx,2),"RSI14":round(rn,1),"Williams":round(wn,1),"Vol3/10":round(v3,2),"TodayValue/10":round(tv,2),"IntraAccel":round(ia,2),"1H%":round(h,2) if not pd.isna(h) else None},None

def main():
    mk=get("/market/all",{"is_details":"false"}) or []
    markets=[(x["market"],x["market"].replace("KRW-","")) for x in mk if x["market"].startswith("KRW-")]
    out={"name":"MAGI Point-in-Time Replay 1.0","method":"v1.5-style full-universe historical replay; no future candles; validation only","targets":[]}
    for ts in TARGETS:
        t=datetime.strptime(ts,"%Y-%m-%d %H:%M:%S").replace(tzinfo=KST); rows=[]; errors=[]
        for i,(market,coin) in enumerate(markets,1):
            row,err=analyse(market,coin,t)
            if row:rows.append(row)
            else:errors.append({"coin":coin,"reason":err})
            time.sleep(.02)
        rows=sorted(rows,key=lambda x:(-x["VPD"],-x["TodayValue/10"]))
        for i,x in enumerate(rows,1):x["Rank"]=i
        focus=[x for x in rows if x["coin"] in FOCUS]
        out["targets"].append({"asof_kst":ts+" KST","analysed":len(rows),"errors":len(errors),"top10":rows[:10],"focus":focus,"focus_errors":[x for x in errors if x["coin"] in FOCUS]})
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
