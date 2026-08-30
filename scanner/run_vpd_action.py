"""GitHub Actions runner for VPD Scanner Integrated v1.5 + WPI5 Shadow."""
import os
import pathlib
import requests
import json
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORK = ROOT / ".runtime"
WORK.mkdir(exist_ok=True)

token = os.environ.get("GITHUB_TOKEN", "").strip()
if not token:
    raise RuntimeError("GITHUB_TOKEN is missing")

source_path = ROOT / "scanner" / "vpd_scanner_v1_5.py"
source = source_path.read_text(encoding="utf-8")

globals_dict = {
    "GITHUB_TOKEN": token,
    "__name__": "__main__",
    "__file__": str(source_path),
    "LOCAL_LATEST_JSON": str(WORK / "vpd_latest.json"),
    "LOCAL_ALL_CSV": str(WORK / "vpd_all_latest.csv"),
    "LOCAL_HISTORY_CSV": str(WORK / "vpd_history.csv"),
}
exec(compile(source, str(source_path), "exec"), globals_dict)

latest_path = WORK / "vpd_latest.json"
if not latest_path.exists():
    raise RuntimeError("vpd_latest.json was not generated")
with latest_path.open("r", encoding="utf-8") as f:
    data = json.load(f)

# WPI5 Shadow must use the exact fresh snapshot generated above, not a stale repository copy.
wpi_script = ROOT / "scanner" / "wpi5_shadow.py"
wpi_source = wpi_script.read_text(encoding="utf-8")
wpi_globals = {
    "__name__": "__main__",
    "__file__": str(wpi_script),
    "LATEST": latest_path,
    "OUT_JSON": WORK / "wpi5_shadow_latest.json",
    "OUT_CSV": WORK / "wpi5_validation_30d.csv",
}
# Patch module constants so main() reads/writes runtime files.
wpi_source = wpi_source.replace("LATEST=ROOT/'data'/'vpd_latest.json'", "LATEST=Path(" + repr(str(latest_path)) + ")")
wpi_source = wpi_source.replace("OUT_JSON=ROOT/'data'/'wpi5_shadow_latest.json'", "OUT_JSON=Path(" + repr(str(WORK / 'wpi5_shadow_latest.json')) + ")")
wpi_source = wpi_source.replace("OUT_CSV=ROOT/'data'/'wpi5_validation_30d.csv'", "OUT_CSV=Path(" + repr(str(WORK / 'wpi5_validation_30d.csv')) + ")")
exec(compile(wpi_source, str(wpi_script), "exec"), wpi_globals)

wpi_path = WORK / "wpi5_shadow_latest.json"
if not wpi_path.exists():
    raise RuntimeError("WPI5 shadow output was not generated")
wpi = json.loads(wpi_path.read_text(encoding="utf-8"))
wpi_by_coin = {x.get("coin"): x for x in wpi.get("today_top10", [])}
for x in data.get("top10", []):
    wx = wpi_by_coin.get(x.get("coin"), {})
    for k in ("WPI5", "LowerDominantDays5", "UpperDominantDays5", "LowerWickTotal5", "UpperWickTotal5", "WPI5Status"):
        x[k] = wx.get(k)
data["wpi5_shadow"] = {
    "name": wpi.get("name"),
    "note": wpi.get("note"),
    "validation_rows": wpi.get("validation_rows"),
    "validation_30d_summary": wpi.get("validation_30d_summary", [])
}
latest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def fmt_num(v, digits=2):
    if v is None: return "-"
    try: return f"{float(v):.{digits}f}"
    except Exception: return str(v)

def wpi_badge(x):
    v=x.get("WPI5"); status=x.get("WPI5Status")
    if v is None: return "WPI5 -"
    icon="🟢" if status=="BULLISH_WICK" else ("🔴" if status=="BEARISH_WICK" else "⚪")
    return f"WPI5 {float(v):+.0f} {icon}"

def build_message(d):
    top=d.get("top10") or []; rockets=d.get("qualified_rockets") or []; triggers=d.get("triggers") or []
    asof=d.get("asof_kst","-")
    try: hour=asof.split()[1][:5]
    except Exception: hour=""
    lines=[f"🔎 VPD 업비트 서치 퍼스트 | {hour or asof}","","📊 TOP10 + WPI5 SHADOW"]
    medals=["🥇","🥈","🥉"]
    for i,x in enumerate(top[:10]):
        prefix=medals[i] if i<3 else f"{i+1}."
        velocity=x.get("VPDVelocity"); vel=""
        if velocity is not None:
            try:
                if float(velocity)>0: vel=f" Δ+{fmt_num(velocity,0)}"
                elif float(velocity)<0: vel=f" Δ{fmt_num(velocity,0)}"
            except Exception: pass
        rocket=" 🚀" if x.get("Rocket") else ""; new=" 🆕" if x.get("NEW_TOP10") else ""
        lines.append(f"{prefix} {x.get('coin','-')} {fmt_num(x.get('VPD'),0)} {x.get('momentum','-')}{vel}{rocket}{new} | Max {fmt_num(x.get('MaxPriceReturn%'))}% | {wpi_badge(x)}")
    bull=[x.get('coin') for x in top if x.get('WPI5Status')=='BULLISH_WICK']; bear=[x.get('coin') for x in top if x.get('WPI5Status')=='BEARISH_WICK']
    lines += ["","🕯 WPI5 SHADOW", "Bull: "+(", ".join(bull) if bull else "없음"), "Bear: "+(", ".join(bear) if bear else "없음")]
    ws=d.get("wpi5_shadow",{}); summary=ws.get("validation_30d_summary",[])
    lines += [f"30D validation: {ws.get('validation_rows','-')} obs"]
    for s in summary:
        lines.append(f"{s.get('group')}: N{s.get('N')} | D3 {fmt_num(s.get('AvgD3%'))}% | Win {fmt_num(s.get('D3WinRate%'),1)}% | MFE5 {fmt_num(s.get('AvgMFE5%'))}%")
    lines += ["","🚀 ROCKET",(" / ".join(x.get("coin","-") for x in rockets[:8]) if rockets else "없음"),"","🚦 TRIGGER"]
    a=[x.get("coin") for x in triggers if x.get("trigger")=="A"]; b=[x.get("coin") for x in triggers if x.get("trigger")=="B"]
    lines.append("A: "+(", ".join(a) if a else "없음")); lines.append("B: "+(", ".join(b) if b else "없음"))
    lines += ["",f"🕒 {asof}","Scanner: VPD v1.5 + WPI5 Shadow | Source: Upbit KRW Spot OHLCV only"]
    return "\n".join(lines)

bot_token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip(); chat_id=os.environ.get("TELEGRAM_CHAT_ID","").strip()
if not bot_token or not chat_id: raise RuntimeError("Telegram secrets are missing")
r=requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",json={"chat_id":chat_id,"text":build_message(data)},timeout=20)
if r.status_code!=200 or not r.json().get("ok"): raise RuntimeError(f"Telegram send failed: {r.status_code} {r.text[:300]}")
print("✅ Telegram sent, message_id:",r.json()["result"]["message_id"])
