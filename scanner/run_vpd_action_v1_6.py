"""Production runner patch for VPD Scanner v1.6.
Keeps VPD v1.5 score/trigger logic unchanged and adds separate VWPI confidence + Distribution Risk POC layers.
Also persists the latest automatic morning/evening Upbit scan state.
"""
import base64
import json
import os
import requests
from pathlib import Path

BASE = Path(__file__).with_name("run_vpd_action.py")
source = BASE.read_text(encoding="utf-8")

source = source.replace(
    'source_path = ROOT / "scanner" / "vpd_scanner_v1_5.py"',
    'source_path = ROOT / "scanner" / "vpd_scanner_v1_6.py"',
    1,
)

old_keys = 'for k in ("WPI5", "LowerDominantDays5", "UpperDominantDays5", "LowerWickTotal5", "UpperWickTotal5", "WPI5Status"):'
new_keys = 'for k in ("WPI5", "VWPI5", "LowerDominantDays5", "UpperDominantDays5", "LowerWickTotal5", "UpperWickTotal5", "VWLowerWeighted5", "VWUpperWeighted5", "WPI5Status"):'
if old_keys not in source:
    raise RuntimeError("v1.6 runner patch failed: WPI merge point not found")
source = source.replace(old_keys, new_keys, 1)

anchor = '''data["wpi5_shadow"] = {
    "name": wpi.get("name"),
    "note": wpi.get("note"),
    "validation_rows": wpi.get("validation_rows"),
    "validation_30d_summary": wpi.get("validation_30d_summary", [])
}
'''
insert = anchor + '''
# v1.6 confidence layers are informational and never change VPD.
def _f(v, default=None):
    try: return float(v)
    except Exception: return default

for x in data.get("top10", []):
    vpd = _f(x.get("VPD"), -999); maxret = _f(x.get("MaxPriceReturn%"), 999); vwpi = _f(x.get("VWPI5"), None)
    eligible = bool(vpd >= 65 and maxret <= 10)
    if not eligible or vwpi is None: conf = "N/A"
    elif vwpi >= 40: conf = "HIGH"
    elif vwpi >= 20: conf = "ENHANCED"
    elif vwpi >= 0: conf = "NORMAL"
    else: conf = "LOW"
    x["VWPIConfidence"] = conf; x["VWPIConfidenceEligible"] = eligible
    giveback = _f(x.get("Giveback%p"), 0.0) or 0.0; turnover = _f(x.get("TodayValue/10"), 0.0) or 0.0
    upper_days = int(_f(x.get("UpperDominantDays5"), 0) or 0); prior_upper_pressure = (vwpi is not None and vwpi < 0) or upper_days >= 3
    if giveback >= 8 and turnover >= 5 and prior_upper_pressure: drisk, reason = "HIGH", "Giveback>=8%p + TodayValue/10>=5x + upper-wick pressure"
    elif giveback >= 5 and turnover >= 3: drisk, reason = "WATCH", "Giveback>=5%p + TodayValue/10>=3x"
    else: drisk, reason = "CLEAR", "-"
    x["DistributionRisk"] = drisk; x["DistributionRiskReason"] = reason

data["v1_6_layers"] = {
    "vpd_score_changed": False,
    "vwpi_confidence_rule": "Only when VPD>=65 AND MaxPriceReturn<=10: VWPI<0 LOW, 0-19 NORMAL, 20-39 ENHANCED, >=40 HIGH",
    "distribution_risk_rule_poc": "HIGH if Giveback>=8%p AND TodayValue/10>=5x AND prior upper-wick pressure; WATCH if Giveback>=5%p AND TodayValue/10>=3x",
    "distribution_risk_note": "POC warning only; does not imply manipulation or intentional distribution"
}
'''
if anchor not in source:
    raise RuntimeError("v1.6 runner patch failed: confidence insertion anchor not found")
source = source.replace(anchor, insert, 1)

old_line = '''        lines.append(f"{prefix} {x.get('coin','-')} {fmt_num(x.get('VPD'),0)} {x.get('momentum','-')}{vel}{rocket}{new} | Max {fmt_num(x.get('MaxPriceReturn%'))}% | {wpi_badge(x)}")'''
new_line = '''        vw=x.get("VWPI5"); vwtxt="VWPI -" if vw is None else f"VWPI {float(vw):+.0f}"
        conf=x.get("VWPIConfidence","N/A"); dr=x.get("DistributionRisk","CLEAR")
        lines.append(f"{prefix} {x.get('coin','-')} {fmt_num(x.get('VPD'),0)} {x.get('momentum','-')}{vel}{rocket}{new} | Max {fmt_num(x.get('MaxPriceReturn%'))}% | {vwtxt} CONF {conf} | DR {dr}")'''
if old_line not in source:
    raise RuntimeError("v1.6 runner patch failed: Telegram TOP10 line not found")
source = source.replace(old_line, new_line, 1)
source = source.replace(
    'lines += ["",f"🕒 {asof}","Scanner: VPD v1.5 + WPI5 Shadow 2.0 | Source: Upbit KRW Spot OHLCV only"]',
    'lines += ["",f"🕒 {asof}","Scanner: VPD v1.6 | VPD score unchanged | VWPI Confidence + Distribution Risk POC | Source: Upbit KRW Spot OHLCV only"]',
    1,
)

exec(compile(source, str(BASE), "exec"), globals())

# Session state: Telegram MAGI1 commands read this snapshot; they do NOT start a scan.
session = os.getenv("MAGI1_SESSION", "").strip().lower()
if session in {"morning", "evening"}:
    latest = Path(__file__).resolve().parents[1] / ".runtime" / "vpd_latest.json"
    state = json.loads(latest.read_text(encoding="utf-8"))
    state["magi1_session"] = session
    state["magi1_state_role"] = "UPBIT_SCAN_STATE"
    text = json.dumps(state, ensure_ascii=False, indent=2)
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo_path = f"data/magi1_upbit_{session}_state.json"
    url = f"https://api.github.com/repos/Henryrotaewon/VPD-Investment/contents/{repo_path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    g = requests.get(url, headers=headers, params={"ref":"main"}, timeout=20)
    payload = {"message": f"Update MAGI1 {session} Upbit scan state", "content": base64.b64encode(text.encode()).decode(), "branch":"main"}
    if g.status_code == 200: payload["sha"] = g.json()["sha"]
    elif g.status_code != 404: raise RuntimeError(f"MAGI1 state read failed: {g.status_code}")
    p = requests.put(url, headers=headers, json=payload, timeout=30)
    if p.status_code not in (200, 201): raise RuntimeError(f"MAGI1 state write failed: {p.status_code} {p.text[:200]}")
    print(f"✅ MAGI1 {session} Upbit scan state persisted: {repo_path}")
