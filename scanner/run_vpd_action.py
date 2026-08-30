"""GitHub Actions runner for VPD Scanner Integrated v1.5 + WPI5 Shadow/Replay."""
import os
import pathlib
import requests
import json
import base64

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
data = json.loads(latest_path.read_text(encoding="utf-8"))

# ------------------------------------------------------------
# WPI5 Shadow v1.0: current TOP10 + simple 30D WPI-only validation
# ------------------------------------------------------------
wpi_script = ROOT / "scanner" / "wpi5_shadow.py"
wpi_source = wpi_script.read_text(encoding="utf-8")
wpi_source = wpi_source.replace("LATEST=ROOT/'data'/'vpd_latest.json'", "LATEST=Path(" + repr(str(latest_path)) + ")")
wpi_source = wpi_source.replace("OUT_JSON=ROOT/'data'/'wpi5_shadow_latest.json'", "OUT_JSON=Path(" + repr(str(WORK / 'wpi5_shadow_latest.json')) + ")")
wpi_source = wpi_source.replace("OUT_CSV=ROOT/'data'/'wpi5_validation_30d.csv'", "OUT_CSV=Path(" + repr(str(WORK / 'wpi5_validation_30d.csv')) + ")")
exec(compile(wpi_source, str(wpi_script), "exec"), {"__name__":"__main__","__file__":str(wpi_script)})

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

# ------------------------------------------------------------
# WPI5 Shadow 2.0: historical replay of VPD + Max + WPI/VWPI
# ------------------------------------------------------------
replay_script = ROOT / "scanner" / "wpi5_historical_replay.py"
replay_source = replay_script.read_text(encoding="utf-8")
replay_source = replay_source.replace('LATEST = ROOT / "data" / "vpd_latest.json"', "LATEST = Path(" + repr(str(latest_path)) + ")")
replay_source = replay_source.replace('OUT_JSON = ROOT / "data" / "wpi5_replay_30d_latest.json"', "OUT_JSON = Path(" + repr(str(WORK / 'wpi5_replay_30d_latest.json')) + ")")
replay_source = replay_source.replace('OUT_CSV = ROOT / "data" / "wpi5_replay_30d.csv"', "OUT_CSV = Path(" + repr(str(WORK / 'wpi5_replay_30d.csv')) + ")")
exec(compile(replay_source, str(replay_script), "exec"), {"__name__":"__main__","__file__":str(replay_script)})

replay_path = WORK / "wpi5_replay_30d_latest.json"
if not replay_path.exists():
    raise RuntimeError("WPI5 historical replay output was not generated")
replay = json.loads(replay_path.read_text(encoding="utf-8"))
data["wpi5_replay_30d"] = {
    "name": replay.get("name"),
    "rows": replay.get("rows"),
    "limitations": replay.get("limitations", []),
    "combination_tests": replay.get("combination_tests", []),
    "key_combo_by_coin": replay.get("key_combo_by_coin", []),
}
latest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# Persist Shadow/Replay outputs so ChatGPT briefings can read them from GitHub later.
def github_put_text(repo_path, text):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/Henryrotaewon/VPD-Investment/contents/{repo_path}"
    g = requests.get(url, headers=headers, params={"ref":"main"}, timeout=20)
    payload = {
        "message": f"Update {repo_path} from VPD WPI5 pipeline",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if g.status_code == 200:
        payload["sha"] = g.json()["sha"]
    elif g.status_code != 404:
        raise RuntimeError(f"GitHub read failed for {repo_path}: {g.status_code} {g.text[:200]}")
    p = requests.put(url, headers=headers, json=payload, timeout=30)
    if p.status_code not in (200, 201):
        raise RuntimeError(f"GitHub write failed for {repo_path}: {p.status_code} {p.text[:300]}")

# Enriched latest JSON + raw validation outputs.
github_put_text("data/vpd_latest.json", latest_path.read_text(encoding="utf-8"))
github_put_text("data/wpi5_shadow_latest.json", wpi_path.read_text(encoding="utf-8"))
github_put_text("data/wpi5_validation_30d.csv", (WORK / "wpi5_validation_30d.csv").read_text(encoding="utf-8-sig"))
github_put_text("data/wpi5_replay_30d_latest.json", replay_path.read_text(encoding="utf-8"))
github_put_text("data/wpi5_replay_30d.csv", (WORK / "wpi5_replay_30d.csv").read_text(encoding="utf-8-sig"))


def fmt_num(v, digits=2):
    if v is None: return "-"
    try: return f"{float(v):.{digits}f}"
    except Exception: return str(v)


def wpi_badge(x):
    v=x.get("WPI5"); status=x.get("WPI5Status")
    if v is None: return "WPI5 -"
    icon="🟢" if status=="BULLISH_WICK" else ("🔴" if status=="BEARISH_WICK" else "⚪")
    return f"WPI5 {float(v):+.0f} {icon}"


def replay_lookup(d, name):
    for x in d.get("wpi5_replay_30d", {}).get("combination_tests", []):
        if x.get("group") == name:
            return x
    return None


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

    rp=d.get("wpi5_replay_30d",{})
    base=replay_lookup(d,"BASE_ALL")
    c1=replay_lookup(d,"VPD65+_MAX6_WPI20+")
    c2=replay_lookup(d,"VPD65+_MAX10_WPI20+")
    c3=replay_lookup(d,"VPD65+_MAX10_VWPI20+")
    lines += ["",f"🧪 30D HISTORICAL REPLAY | {rp.get('rows','-')} rows"]
    for label,x in (("BASE",base),("V65 Max≤6 WPI20+",c1),("V65 Max≤10 WPI20+",c2),("V65 Max≤10 VWPI20+",c3)):
        if x:
            lines.append(f"{label}: N{x.get('N')} | D3 {fmt_num(x.get('AvgD3%'))}% | Win {fmt_num(x.get('D3WinRate%'),1)}% | MFE {fmt_num(x.get('AvgMFE5%'))}% | MAE {fmt_num(x.get('AvgMAE5%'))}%")

    lines += ["","🚀 ROCKET",(" / ".join(x.get("coin","-") for x in rockets[:8]) if rockets else "없음"),"","🚦 TRIGGER"]
    a=[x.get("coin") for x in triggers if x.get("trigger")=="A"]; b=[x.get("coin") for x in triggers if x.get("trigger")=="B"]
    lines.append("A: "+(", ".join(a) if a else "없음")); lines.append("B: "+(", ".join(b) if b else "없음"))
    lines += ["",f"🕒 {asof}","Scanner: VPD v1.5 + WPI5 Shadow 2.0 | Source: Upbit KRW Spot OHLCV only"]
    return "\n".join(lines)


bot_token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip(); chat_id=os.environ.get("TELEGRAM_CHAT_ID","").strip()
if not bot_token or not chat_id: raise RuntimeError("Telegram secrets are missing")
r=requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",json={"chat_id":chat_id,"text":build_message(data)},timeout=20)
if r.status_code!=200 or not r.json().get("ok"):
    raise RuntimeError(f"Telegram send failed: {r.status_code} {r.text[:300]}")
print("✅ Telegram sent, message_id:",r.json()["result"]["message_id"])
