# ============================================================
# VPD Scanner Integrated v1.4
# SOURCE OF TRUTH: Upbit KRW Spot OHLCV ONLY
# Migrated from the user's Colab source without changing VPD scoring logic.
# ============================================================

import io
import json
import time
import base64
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
from getpass import getpass

KST = ZoneInfo("Asia/Seoul")
BASE_URL = "https://api.upbit.com/v1"
OWNER = "Henryrotaewon"
REPO = "VPD-Investment"
BRANCH = "main"
DAILY_COUNT = 35
API_SLEEP = 0.12
GITHUB_LATEST_JSON = "data/vpd_latest.json"
GITHUB_ALL_CSV = "data/vpd_all_latest.csv"
GITHUB_HISTORY_CSV = "data/vpd_history.csv"
LOCAL_LATEST_JSON = "/content/vpd_latest.json"
LOCAL_ALL_CSV = "/content/vpd_all_latest.csv"
LOCAL_HISTORY_CSV = "/content/vpd_history.csv"
RAW_BASE = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}/data"

try:
    GITHUB_TOKEN
except NameError:
    print("🔐 GitHub Token이 현재 런타임에 없습니다.")
    GITHUB_TOKEN = getpass("GitHub Token 입력: ")
if not GITHUB_TOKEN:
    raise RuntimeError("GitHub Token이 없습니다.")
GITHUB_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
repo_check = requests.get(f"https://api.github.com/repos/{OWNER}/{REPO}", headers=GITHUB_HEADERS, timeout=15)
if repo_check.status_code != 200:
    raise RuntimeError(f"GitHub 인증 실패: {repo_check.status_code} {repo_check.text[:300]}")
print("✅ GitHub 인증:", repo_check.json()["full_name"])

def raw_url(filename): return f"{RAW_BASE}/{filename}?t={int(time.time())}"
def read_public_csv(filename):
    try:
        r=requests.get(raw_url(filename),timeout=20)
        if r.status_code==200: return pd.read_csv(io.BytesIO(r.content))
    except Exception as e: print(f"⚠️ CSV 읽기 오류 {filename}:",e)
    return None

def read_public_json(filename):
    try:
        r=requests.get(raw_url(filename),timeout=20)
        if r.status_code==200: return r.json()
    except Exception as e: print(f"⚠️ JSON 읽기 오류 {filename}:",e)
    return None

PREV_ALL=read_public_csv("vpd_all_latest.csv")
PREV_LATEST=read_public_json("vpd_latest.json")
if PREV_ALL is not None and len(PREV_ALL)>0: print(f"✅ 이전 VPD Snapshot 로드: {len(PREV_ALL)}종목")
else:
    print("⚠️ 이전 전체 Snapshot 없음 → Baseline 모드")
    PREV_ALL=pd.DataFrame()
PREV_TOP10_SET=set()
if PREV_LATEST and isinstance(PREV_LATEST.get("top10"),list):
    for x in PREV_LATEST["top10"]:
        coin=x.get("coin")
        if coin: PREV_TOP10_SET.add(coin)
print("✅ 이전 TOP10:",", ".join(sorted(PREV_TOP10_SET)) if PREV_TOP10_SET else "없음")
PREV_SCAN_TIME=None
SCAN_INTERVAL_HOURS=np.nan
if PREV_LATEST:
    prev_time_str=PREV_LATEST.get("asof") or PREV_LATEST.get("asof_kst")
    if prev_time_str:
        try:
            PREV_SCAN_TIME=pd.Timestamp(prev_time_str)
            if PREV_SCAN_TIME.tzinfo is None: PREV_SCAN_TIME=PREV_SCAN_TIME.tz_localize("Asia/Seoul")
        except Exception: PREV_SCAN_TIME=None

session=requests.Session(); session.headers.update({"Accept":"application/json","User-Agent":"VPD-Scanner/1.4"})
def upbit_get(url,params=None,retry=3):
    for attempt in range(retry):
        try:
            r=session.get(url,params=params,timeout=10)
            if r.status_code==200: return r.json()
            if r.status_code==429:
                time.sleep(1.0+attempt); continue
            time.sleep(0.5)
        except Exception: time.sleep(0.5)
    return None

def get_krw_markets():
    data=upbit_get(f"{BASE_URL}/market/all",{"is_details":"false"})
    if not data: raise RuntimeError("업비트 KRW 마켓 조회 실패")
    return [{"market":i["market"],"coin":i["market"].replace("KRW-",""),"korean_name":i.get("korean_name","")} for i in data if i["market"].startswith("KRW-")]

def get_daily_candles(market,count=DAILY_COUNT):
    data=upbit_get(f"{BASE_URL}/candles/days",{"market":market,"count":count})
    if not data: return None
    return pd.DataFrame(data).sort_values("candle_date_time_kst").reset_index(drop=True)

def get_1h_return(market):
    data=upbit_get(f"{BASE_URL}/candles/minutes/60",{"market":market,"count":2})
    if not data or len(data)<2: return np.nan
    current=float(data[0]["trade_price"]); previous=float(data[1]["trade_price"])
    return np.nan if previous==0 else (current/previous-1)*100

def calc_rsi(close,period=14):
    close=pd.Series(close).astype(float); delta=close.diff(); gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    avg_gain=gain.ewm(alpha=1/period,adjust=False).mean(); avg_loss=loss.ewm(alpha=1/period,adjust=False).mean(); rs=avg_gain/avg_loss.replace(0,np.nan)
    return 100-100/(1+rs)

def calc_williams(df,period=14):
    high=df["high_price"].rolling(period).max(); low=df["low_price"].rolling(period).min(); close=df["trade_price"]; denominator=(high-low).replace(0,np.nan)
    return -100*(high-close)/denominator

def safe_ratio(a,b):
    try:
        if pd.isna(a) or pd.isna(b) or b==0: return np.nan
        return float(a)/float(b)
    except Exception: return np.nan

def score_vpd(vol3_10,today_value_10,intra_accel,rsi,williams,day_return,spike_collapse):
    score=0.0
    if not pd.isna(vol3_10):
        if vol3_10>=3.0: score+=25
        elif vol3_10>=2.0: score+=21
        elif vol3_10>=1.5: score+=17
        elif vol3_10>=1.2: score+=12
        elif vol3_10>=1.0: score+=7
    if not pd.isna(today_value_10):
        if today_value_10>=3.0: score+=20
        elif today_value_10>=2.0: score+=17
        elif today_value_10>=1.2: score+=13
        elif today_value_10>=0.8: score+=9
        elif today_value_10>=0.4: score+=5
    if not pd.isna(intra_accel):
        if intra_accel>=4: score+=15
        elif intra_accel>=2.5: score+=12
        elif intra_accel>=1.5: score+=9
        elif intra_accel>=1.0: score+=5
    if not pd.isna(rsi):
        if 45<=rsi<=65: score+=15
        elif 35<=rsi<45: score+=12
        elif 65<rsi<=72: score+=10
        elif 30<=rsi<35: score+=8
        elif 72<rsi<=78: score+=5
    if not pd.isna(williams):
        if -80<=williams<=-40: score+=10
        elif -40<williams<=-20: score+=8
        elif -90<=williams<-80: score+=7
        elif -20<williams<=-10: score+=4
    if not pd.isna(day_return):
        abs_r=abs(day_return)
        if abs_r<=2: score+=15
        elif abs_r<=4: score+=13
        elif abs_r<=6: score+=10
        elif abs_r<=10: score+=6
        elif abs_r<=15: score+=2
    if spike_collapse: score-=15
    return round(max(0,min(100,score)),1)

def calc_momentum(rsi_now,rsi_prev,william_now,william_prev,hour_return,vol3_10):
    points=0
    if not pd.isna(rsi_now) and not pd.isna(rsi_prev):
        d=rsi_now-rsi_prev
        if d>=5: points+=2
        elif d>0: points+=1
    if not pd.isna(william_now) and not pd.isna(william_prev):
        dw=william_now-william_prev
        if dw>=15: points+=2
        elif dw>0: points+=1
    if not pd.isna(hour_return):
        if 0<hour_return<=3: points+=1
        elif 3<hour_return<=6: points+=2
    if not pd.isna(vol3_10):
        if vol3_10>=2: points+=2
        elif vol3_10>=1.2: points+=1
    if points>=6: return "↑↑"
    elif points>=3: return "↑"
    elif points>=1: return "→"
    return "↓"

def analyse_coin(market,coin):
    df=get_daily_candles(market)
    if df is None: return None,"NO_DAILY_DATA"
    if len(df)<20: return None,f"SHORT_HISTORY_{len(df)}"
    df["RSI14"]=calc_rsi(df["trade_price"]); df["Williams"]=calc_williams(df)
    latest=df.iloc[-1]; prev=df.iloc[-2]; price=float(latest["trade_price"]); prev_close=float(prev["trade_price"]); day_return=(price/prev_close-1)*100
    hour_return=get_1h_return(market); time.sleep(API_SLEEP)
    volume=df["candle_acc_trade_volume"].astype(float); value=df["candle_acc_trade_price"].astype(float)
    vol3=volume.iloc[-3:].mean(); vol10=volume.iloc[-11:-1].mean(); vol3_10=safe_ratio(vol3,vol10)
    today_value=value.iloc[-1]; value10=value.iloc[-11:-1].mean(); today_value_10=safe_ratio(today_value,value10)
    prev3_value=value.iloc[-4:-1].mean(); intra_accel=safe_ratio(today_value,prev3_value)
    previous_values=value.iloc[-11:-1]; median_val=previous_values.median(); max_val=previous_values.max(); spike_collapse=False
    if median_val>0 and max_val>0:
        historical_spike=(max_val/median_val)>=5; current_collapse=(today_value/max_val)<=0.25; spike_collapse=bool(historical_spike and current_collapse)
    rsi_now=float(df["RSI14"].iloc[-1]); rsi_prev=float(df["RSI14"].iloc[-2]); william_now=float(df["Williams"].iloc[-1]); william_prev=float(df["Williams"].iloc[-2])
    vpd=score_vpd(vol3_10,today_value_10,intra_accel,rsi_now,william_now,day_return,spike_collapse)
    momentum=calc_momentum(rsi_now,rsi_prev,william_now,william_prev,hour_return,vol3_10)
    trigger="-"
    if vpd>=85 and momentum in ["↑","↑↑"]: trigger="A"
    elif 75<=vpd<85 and momentum=="↑↑" and abs(day_return)<=6: trigger="B"
    row={"coin":coin,"market":market,"price":round(price,8),"VPD":vpd,"momentum":momentum,"trigger":trigger,"1D%":round(day_return,2),"1H%":round(hour_return,2) if not pd.isna(hour_return) else np.nan,"RSI14":round(rsi_now,1),"Williams":round(william_now,1),"Vol3/10":round(vol3_10,2) if not pd.isna(vol3_10) else np.nan,"TodayValue/10":round(today_value_10,2) if not pd.isna(today_value_10) else np.nan,"IntraAccel":round(intra_accel,2) if not pd.isna(intra_accel) else np.nan,"SpikeCollapse":spike_collapse}
    return row,None

scan_start=datetime.now(KST)
if PREV_SCAN_TIME is not None:
    try: SCAN_INTERVAL_HOURS=(pd.Timestamp(scan_start)-PREV_SCAN_TIME).total_seconds()/3600
    except Exception: SCAN_INTERVAL_HOURS=np.nan
print("\n🚀 VPD Scanner v1.4 시작"); print(scan_start.strftime("%Y-%m-%d %H:%M:%S KST"))
if not pd.isna(SCAN_INTERVAL_HOURS): print("직전 스캔 간격:",round(SCAN_INTERVAL_HOURS,2),"시간")
markets=get_krw_markets(); print(f"KRW 종목: {len(markets)}")
results=[]; excluded=[]
for idx,item in enumerate(markets,start=1):
    market=item["market"]; coin=item["coin"]
    try:
        row,reason=analyse_coin(market,coin)
        if row is not None: results.append(row)
        else: excluded.append({"coin":coin,"market":market,"reason":reason})
    except Exception as e: excluded.append({"coin":coin,"market":market,"reason":str(e)[:100]})
    time.sleep(API_SLEEP)
    if idx%25==0 or idx==len(markets): print(f"진행 {idx}/{len(markets)}")
if len(results)==0: raise RuntimeError("Scanner 결과가 없습니다.")
CURRENT=pd.DataFrame(results).sort_values(["VPD","TodayValue/10"],ascending=[False,False]).reset_index(drop=True); CURRENT.insert(0,"Rank",range(1,len(CURRENT)+1))
CURRENT["PrevRank"]=np.nan; CURRENT["PrevVPD"]=np.nan
if PREV_ALL is not None and len(PREV_ALL)>0 and "coin" in PREV_ALL.columns:
    prev_lookup=PREV_ALL.drop_duplicates(subset=["coin"]).set_index("coin")
    if "Rank" in prev_lookup.columns: CURRENT["PrevRank"]=CURRENT["coin"].map(prev_lookup["Rank"])
    if "VPD" in prev_lookup.columns: CURRENT["PrevVPD"]=CURRENT["coin"].map(prev_lookup["VPD"])
CURRENT["PrevRank"]=pd.to_numeric(CURRENT["PrevRank"],errors="coerce"); CURRENT["PrevVPD"]=pd.to_numeric(CURRENT["PrevVPD"],errors="coerce")
CURRENT["VPDVelocity"]=CURRENT["VPD"]-CURRENT["PrevVPD"]
if not pd.isna(SCAN_INTERVAL_HOURS) and SCAN_INTERVAL_HOURS>0.05: CURRENT["VPDVelocityPerHour"]=CURRENT["VPDVelocity"]/SCAN_INTERVAL_HOURS
else: CURRENT["VPDVelocityPerHour"]=np.nan
CURRENT["RankChange"]=CURRENT["PrevRank"]-CURRENT["Rank"]
CURRENT["NEW_MARKET"]=CURRENT["PrevRank"].isna(); CURRENT["NEW_TOP10"]=False
if PREV_TOP10_SET: CURRENT["NEW_TOP10"]=(CURRENT["Rank"]<=10)&(~CURRENT["coin"].isin(PREV_TOP10_SET))

def rocket_logic(row):
    if row["VPD"]<50: return False,"-"
    velocity_flag=not pd.isna(row["VPDVelocity"]) and row["VPDVelocity"]>=10
    rank_flag=not pd.isna(row["RankChange"]) and row["RankChange"]>=10
    if velocity_flag and rank_flag: return True,"VELOCITY+RANK"
    if velocity_flag: return True,"VELOCITY"
    if rank_flag: return True,"RANK"
    return False,"-"
rocket_result=CURRENT.apply(rocket_logic,axis=1); CURRENT["Rocket"]=[x[0] for x in rocket_result]; CURRENT["RocketReason"]=[x[1] for x in rocket_result]
def make_status(row):
    new_market=bool(row["NEW_MARKET"]); new_top10=bool(row["NEW_TOP10"]); rocket=bool(row["Rocket"])
    if new_market and rocket: return "NEW_MARKET🚀"
    if new_top10 and rocket: return "NEW_TOP10🚀"
    if new_market: return "NEW_MARKET"
    if new_top10: return "NEW_TOP10"
    if rocket: return "🚀"
    return "-"
CURRENT["Status"]=CURRENT.apply(make_status,axis=1)
for col in ["PrevVPD","VPDVelocity","VPDVelocityPerHour"]: CURRENT[col]=CURRENT[col].round(2)
CURRENT["RankChange"]=CURRENT["RankChange"].round(0)
VPD_ALL=CURRENT.copy(); VPD_TOP10=VPD_ALL.head(10).copy(); TOP10=VPD_TOP10
print("\n📊 VPD TOP10")
display_cols=["Rank","coin","price","VPD","PrevVPD","VPDVelocity","VPDVelocityPerHour","PrevRank","RankChange","momentum","Status","RocketReason","trigger","1D%","1H%","RSI14","Williams","Vol3/10","TodayValue/10","IntraAccel","SpikeCollapse"]
print(VPD_TOP10[display_cols].to_string(index=False))
rocket_df=VPD_ALL[VPD_ALL["Rocket"]==True].copy(); new_top10_df=VPD_TOP10[VPD_TOP10["NEW_TOP10"]==True]; trigger_df=VPD_ALL[VPD_ALL["trigger"].isin(["A","B"])]

def df_to_json_records(df):
    safe=df.astype(object).where(pd.notnull(df),None); return safe.to_dict(orient="records")
now=datetime.now(KST)
result_json={"scanner":"VPD Scanner Integrated v1.4","asof":now.isoformat(),"asof_kst":now.strftime("%Y-%m-%d %H:%M:%S KST"),"previous_asof":str(PREV_SCAN_TIME) if PREV_SCAN_TIME is not None else None,"scan_interval_hours":round(float(SCAN_INTERVAL_HOURS),3) if not pd.isna(SCAN_INTERVAL_HOURS) else None,"universe":"UPBIT_KRW","market_count":len(markets),"analysed_count":len(VPD_ALL),"excluded_count":len(excluded),"source_of_truth":"UPBIT_KRW_OHLCV","vpd_rule":"Upbit KRW Spot only","velocity_rule":"current VPD - previous VPD","velocity_per_hour_rule":"VPDVelocity / scan interval hours","rocket_rule":"VPD >= 50 AND (VPDVelocity >= 10 OR RankChange >= 10)","top10":df_to_json_records(VPD_TOP10),"qualified_rockets":df_to_json_records(rocket_df.head(30)),"new_top10":df_to_json_records(new_top10_df),"triggers":df_to_json_records(trigger_df),"excluded":excluded}
with open(LOCAL_LATEST_JSON,"w",encoding="utf-8") as f: json.dump(result_json,f,ensure_ascii=False,indent=2,default=str)
VPD_ALL.to_csv(LOCAL_ALL_CSV,index=False,encoding="utf-8-sig")
OLD_HISTORY=read_public_csv("vpd_history.csv"); history_current=VPD_ALL.copy(); history_current.insert(0,"scan_time",now.isoformat()); history_current.insert(1,"scan_interval_hours",round(float(SCAN_INTERVAL_HOURS),3) if not pd.isna(SCAN_INTERVAL_HOURS) else np.nan)
HISTORY=pd.concat([OLD_HISTORY,history_current],ignore_index=True) if OLD_HISTORY is not None and len(OLD_HISTORY)>0 else history_current
if "scan_time" in HISTORY.columns and "coin" in HISTORY.columns: HISTORY=HISTORY.drop_duplicates(subset=["scan_time","coin"],keep="last")
HISTORY.to_csv(LOCAL_HISTORY_CSV,index=False,encoding="utf-8-sig")

def github_upload(local_path,repo_path):
    url=f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{repo_path}"; check=requests.get(url,headers=GITHUB_HEADERS,params={"ref":BRANCH},timeout=20); sha=None
    if check.status_code==200: sha=check.json().get("sha")
    elif check.status_code!=404: raise RuntimeError(f"{repo_path} 조회 실패: {check.status_code} {check.text[:300]}")
    with open(local_path,"rb") as f: encoded=base64.b64encode(f.read()).decode("utf-8")
    payload={"message":f"VPD Scanner v1.4 update {repo_path}","content":encoded,"branch":BRANCH}
    if sha: payload["sha"]=sha
    r=requests.put(url,headers=GITHUB_HEADERS,json=payload,timeout=60)
    if r.status_code in (200,201): print(f"✅ GitHub: {repo_path}"); return True
    print(f"❌ GitHub: {repo_path}",r.status_code,r.text[:500]); return False

print("\n📤 GitHub 저장 시작")
ok1=github_upload(LOCAL_LATEST_JSON,GITHUB_LATEST_JSON); ok2=github_upload(LOCAL_ALL_CSV,GITHUB_ALL_CSV); ok3=github_upload(LOCAL_HISTORY_CSV,GITHUB_HISTORY_CSV)
scan_end=datetime.now(KST); elapsed=(scan_end-scan_start).total_seconds()
print("\n✅ VPD Scanner v1.4 완료")
print("KRW 마켓:",len(markets),"분석 완료:",len(VPD_ALL),"분석 제외:",len(excluded),"TOP10:",len(VPD_TOP10),"Qualified Rockets:",len(rocket_df),"NEW TOP10:",len(new_top10_df),"History rows:",len(HISTORY))
print("소요시간:",round(elapsed,1),"초")
print("GitHub:","latest.json",("OK" if ok1 else "FAIL"),"all_latest",("OK" if ok2 else "FAIL"),"history",("OK" if ok3 else "FAIL"))
