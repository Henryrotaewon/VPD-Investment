"""GitHub Actions runner for VPD Scanner Integrated v1.4 + MaxPrice POC.
Preserves the canonical v1.4 source and applies the approved price-reaction correction at runtime:
price non-overheating uses today's intraday MAX (high/current) vs previous close, not current return alone.
"""
import os
import pathlib
import requests
import json

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORK = ROOT / ".runtime"
WORK.mkdir(exist_ok=True)

token = os.environ.get("GITHUB_TOKEN", "").strip()
if not token:
    raise RuntimeError("GITHUB_TOKEN is missing")

source_path = ROOT / "scanner" / "vpd_scanner_v1_4.py"
source = source_path.read_text(encoding="utf-8")
source = source.replace('LOCAL_LATEST_JSON = "/content/vpd_latest.json"', f'LOCAL_LATEST_JSON = {str(WORK / "vpd_latest.json")!r}')
source = source.replace('LOCAL_ALL_CSV = "/content/vpd_all_latest.csv"', f'LOCAL_ALL_CSV = {str(WORK / "vpd_all_latest.csv")!r}')
source = source.replace('LOCAL_HISTORY_CSV = "/content/vpd_history.csv"', f'LOCAL_HISTORY_CSV = {str(WORK / "vpd_history.csv")!r}')

# ------------------------------------------------------------
# MaxPrice POC correction
# Old v1.4: price non-overheating = abs(current / prev_close - 1)
# New POC : price non-overheating = max(today_high, current) / prev_close - 1
# This prevents a coin that already spiked intraday and retraced from being
# misclassified as "price has not reacted yet".
# The original v1.4 source file remains untouched for A/B validation.
# ------------------------------------------------------------
old_line = 'latest=df.iloc[-1]; prev=df.iloc[-2]; price=float(latest["trade_price"]); prev_close=float(prev["trade_price"]); day_return=(price/prev_close-1)*100'
new_line = 'latest=df.iloc[-1]; prev=df.iloc[-2]; price=float(latest["trade_price"]); prev_close=float(prev["trade_price"]); day_return=(price/prev_close-1)*100; day_high=float(latest["high_price"]); max_price=max(price,day_high); max_price_return=(max_price/prev_close-1)*100; giveback_pct_point=max_price_return-day_return'
if old_line not in source:
    raise RuntimeError("MaxPrice POC patch failed: analyse_coin signature line not found")
source = source.replace(old_line, new_line, 1)

old_score = 'vpd=score_vpd(vol3_10,today_value_10,intra_accel,rsi_now,william_now,day_return,spike_collapse)'
new_score = 'vpd=score_vpd(vol3_10,today_value_10,intra_accel,rsi_now,william_now,max_price_return,spike_collapse)'
if old_score not in source:
    raise RuntimeError("MaxPrice POC patch failed: score call not found")
source = source.replace(old_score, new_score, 1)

# Trigger B's price-muted condition must use the same corrected price reaction.
source = source.replace('elif 75<=vpd<85 and momentum=="↑↑" and abs(day_return)<=6: trigger="B"', 'elif 75<=vpd<85 and momentum=="↑↑" and abs(max_price_return)<=6: trigger="B"', 1)

# Persist both current return and intraday maximum reaction for validation.
old_row = '"1D%":round(day_return,2),"1H%"'
new_row = '"1D%":round(day_return,2),"MaxPriceReturn%":round(max_price_return,2),"Giveback%p":round(giveback_pct_point,2),"DayHigh":round(day_high,8),"1H%"'
if old_row not in source:
    raise RuntimeError("MaxPrice POC patch failed: output row insertion point not found")
source = source.replace(old_row, new_row, 1)

# Make the produced snapshot self-describing without changing the canonical source.
source = source.replace('"scanner":"VPD Scanner Integrated v1.4"', '"scanner":"VPD Scanner Integrated v1.4 + MaxPrice POC"')

globals_dict = {"GITHUB_TOKEN": token, "__name__": "__main__", "__file__": str(source_path)}
exec(compile(source, str(source_path), "exec"), globals_dict)

latest_path = WORK / "vpd_latest.json"
if not latest_path.exists():
    raise RuntimeError("vpd_latest.json was not generated")
with latest_path.open("r", encoding="utf-8") as f:
    data = json.load(f)

def fmt_num(v, digits=2):
    if v is None: return "-"
    try: return f"{float(v):.{digits}f}"
    except Exception: return str(v)

def build_message(d):
    top=d.get("top10") or []; rockets=d.get("qualified_rockets") or []; triggers=d.get("triggers") or []
    asof=d.get("asof_kst","-")
    try: hour=asof.split()[1][:5]
    except Exception: hour=""
    lines=[f"🔎 VPD 업비트 서치 퍼스트 | {hour or asof}","","📊 TOP10"]
    medals=["🥇","🥈","🥉"]
    for i,x in enumerate(top[:10]):
        prefix=medals[i] if i<3 else f"{i+1}."
        velocity=x.get("VPDVelocity"); vel=""
        if velocity is not None:
            try:
                if float(velocity)>0: vel=f"  Δ+{fmt_num(velocity,0)}"
                elif float(velocity)<0: vel=f"  Δ{fmt_num(velocity,0)}"
            except Exception: pass
        rocket=" 🚀" if x.get("Rocket") else ""; new=" 🆕" if x.get("NEW_TOP10") else ""
        maxret=x.get("MaxPriceReturn%")
        max_txt=f" | Max {fmt_num(maxret)}%" if maxret is not None else ""
        lines.append(f"{prefix} {x.get('coin','-')}  {fmt_num(x.get('VPD'),0)}  {x.get('momentum','-')}{vel}{rocket}{new}{max_txt}")
    lines += ["","🚀 ROCKET",(" / ".join(x.get("coin","-") for x in rockets[:8]) if rockets else "없음"),"","🚦 TRIGGER"]
    a=[x.get("coin") for x in triggers if x.get("trigger")=="A"]; b=[x.get("coin") for x in triggers if x.get("trigger")=="B"]
    lines.append("A: "+(", ".join(a) if a else "없음")); lines.append("B: "+(", ".join(b) if b else "없음"))
    if top:
        lead=top[0]
        lines += ["","🎯 LEAD",f"{lead.get('coin','-')} VPD {fmt_num(lead.get('VPD'),0)} | 1D {fmt_num(lead.get('1D%'))}% | Max {fmt_num(lead.get('MaxPriceReturn%'))}% | Giveback {fmt_num(lead.get('Giveback%p'))}%p",f"Vol3/10 {fmt_num(lead.get('Vol3/10'))} | IntraAccel {fmt_num(lead.get('IntraAccel'))}"]
    lines += ["",f"🕒 {asof}","Source: Upbit KRW Spot OHLCV only | MaxPrice POC"]
    return "\n".join(lines)

bot_token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip(); chat_id=os.environ.get("TELEGRAM_CHAT_ID","").strip()
if not bot_token or not chat_id: raise RuntimeError("Telegram secrets are missing")
r=requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",json={"chat_id":chat_id,"text":build_message(data)},timeout=20)
if r.status_code!=200 or not r.json().get("ok"): raise RuntimeError(f"Telegram send failed: {r.status_code} {r.text[:300]}")
print("✅ Telegram sent, message_id:",r.json()["result"]["message_id"])
