"""GitHub Actions runner for VPD Scanner Integrated v1.4.

This wrapper keeps the original v1.4 calculation source unchanged. It adapts
GitHub Actions credentials/paths, executes the original source, then sends a
compact Telegram radar. VPD scoring/momentum/trigger/rocket calculations are
not changed here.
"""
import os
import pathlib
import runpy
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORK = ROOT / ".runtime"
WORK.mkdir(exist_ok=True)

# Original Colab source expects these globals and /content paths.
token = os.environ.get("GITHUB_TOKEN", "").strip()
if not token:
    raise RuntimeError("GITHUB_TOKEN is missing")

# The source writes /content/*. On GitHub-hosted Ubuntu runners /content can be
# created; keeping those paths avoids changing calculation/persistence logic.
pathlib.Path("/content").mkdir(parents=True, exist_ok=True)

# Inject token so original source never enters getpass().
runpy.run_path(
    str(ROOT / "scanner" / "vpd_scanner_v1_4.py"),
    init_globals={"GITHUB_TOKEN": token},
    run_name="__main__",
)

# Load the just-generated latest snapshot for Telegram.
latest_path = pathlib.Path("/content/vpd_latest.json")
if not latest_path.exists():
    raise RuntimeError("vpd_latest.json was not generated")

import json
with latest_path.open("r", encoding="utf-8") as f:
    data = json.load(f)

def fmt_num(v, digits=2):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)

def build_message(d):
    top = d.get("top10") or []
    rockets = d.get("qualified_rockets") or []
    triggers = d.get("triggers") or []
    asof = d.get("asof_kst", "-")
    hour = ""
    try:
        hour = asof.split()[1][:5]
    except Exception:
        pass
    title = "🌅 VPD MORNING" if hour and hour < "12:00" else "🌙 VPD EVENING"
    lines = [f"{title} | {hour or asof}", "", "📊 TOP10"]
    medals = ["🥇", "🥈", "🥉"]
    for i, x in enumerate(top[:10]):
        prefix = medals[i] if i < 3 else f"{i+1}."
        velocity = x.get("VPDVelocity")
        vel = ""
        if velocity is not None:
            try:
                if float(velocity) > 0:
                    vel = f"  Δ+{fmt_num(velocity,0)}"
                elif float(velocity) < 0:
                    vel = f"  Δ{fmt_num(velocity,0)}"
            except Exception:
                pass
        rocket = " 🚀" if x.get("Rocket") else ""
        new = " 🆕" if x.get("NEW_TOP10") else ""
        lines.append(f"{prefix} {x.get('coin','-')}  {fmt_num(x.get('VPD'),0)}  {x.get('momentum','-')}{vel}{rocket}{new}")

    lines += ["", "🚀 ROCKET"]
    if rockets:
        lines.append(" / ".join(x.get("coin", "-") for x in rockets[:8]))
    else:
        lines.append("없음")

    lines += ["", "🚦 TRIGGER"]
    a = [x.get("coin") for x in triggers if x.get("trigger") == "A"]
    b = [x.get("coin") for x in triggers if x.get("trigger") == "B"]
    lines.append("A: " + (", ".join(a) if a else "없음"))
    lines.append("B: " + (", ".join(b) if b else "없음"))

    if top:
        lead = top[0]
        lines += ["", "🎯 LEAD", f"{lead.get('coin','-')} VPD {fmt_num(lead.get('VPD'),0)} | 1D {fmt_num(lead.get('1D%'))}% | 1H {fmt_num(lead.get('1H%'))}%", f"Vol3/10 {fmt_num(lead.get('Vol3/10'))} | IntraAccel {fmt_num(lead.get('IntraAccel'))}"]

    lines += ["", f"🕒 {asof}", "Source: Upbit KRW Spot OHLCV only"]
    return "\n".join(lines)

bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not bot_token or not chat_id:
    raise RuntimeError("Telegram secrets are missing")

text = build_message(data)
r = requests.post(
    f"https://api.telegram.org/bot{bot_token}/sendMessage",
    json={"chat_id": chat_id, "text": text},
    timeout=20,
)
if r.status_code != 200 or not r.json().get("ok"):
    raise RuntimeError(f"Telegram send failed: {r.status_code} {r.text[:300]}")
print("✅ Telegram sent, message_id:", r.json()["result"]["message_id"])
