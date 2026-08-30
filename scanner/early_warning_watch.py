"""VPD Early Warning POC.

This layer is deliberately separate from VPD v1.4.
It reads the latest Upbit-KRW VPD snapshot, selects a small watchlist, and
checks short-horizon Upbit KRW minute-candle turnover acceleration every run.
It does NOT change VPD scores, triggers, momentum, or rocket rules.

Telegram alerts are sent only when a coin enters ORANGE/RED, escalates from
ORANGE to RED, or clears an existing alert. WATCH/QUIET observations do not
spam Telegram.
"""

import csv
import io
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
OWNER = "Henryrotaewon"
REPO = "VPD-Investment"
BRANCH = "main"
BASE_URL = "https://api.upbit.com/v1"
RAW_BASE = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}/data"

LATEST_PATH = "data/vpd_latest.json"
WATCHLIST_PATH = "data/watchlist.json"
STATE_PATH = "data/early_warning_state.json"
HISTORY_PATH = "data/early_warning_history.csv"

MAX_WATCH = 8
MAX_SNAPSHOT_AGE_HOURS = 16.0
API_SLEEP = 0.12

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN is missing")
if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Telegram secrets are missing")

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "VPD-Early-Warning/0.1"})


def raw_url(filename):
    return f"{RAW_BASE}/{filename}?t={int(time.time())}"


def read_public_json(filename, default=None):
    try:
        r = requests.get(raw_url(filename), timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"WARN json read {filename}: {e}")
    return default


def read_public_text(filename):
    try:
        r = requests.get(raw_url(filename), timeout=20)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"WARN text read {filename}: {e}")
    return None


def github_put_text(path, text, message):
    """Update/create only when UTF-8 content actually changed."""
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    check = requests.get(url, headers=GH_HEADERS, params={"ref": BRANCH}, timeout=20)
    sha = None
    old_text = None
    if check.status_code == 200:
        meta = check.json()
        sha = meta.get("sha")
        # Raw read is easier and avoids base64 edge cases.
        old_text = read_public_text(path.replace("data/", "")) if path.startswith("data/") else None
    elif check.status_code != 404:
        raise RuntimeError(f"GitHub read failed {path}: {check.status_code} {check.text[:200]}")

    if old_text is not None and old_text.strip() == text.strip():
        print(f"UNCHANGED {path}")
        return False

    import base64
    payload = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=GH_HEADERS, json=payload, timeout=45)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub write failed {path}: {r.status_code} {r.text[:300]}")
    print(f"UPDATED {path}")
    return True


def parse_asof(value):
    if not value:
        return None
    s = str(value).replace(" KST", "+09:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return None


def fnum(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def watch_flags(x):
    """Transparent POC selection flags; these are NOT VPD scoring rules."""
    return {
        "vpd75": fnum(x.get("VPD")) >= 75,
        "velocity": fnum(x.get("VPDVelocityPerHour"), -999) >= 0.75,
        "today_value": fnum(x.get("TodayValue/10")) >= 2.0,
        "intra_accel": fnum(x.get("IntraAccel")) >= 2.5,
        "new_top10": bool(x.get("NEW_TOP10")),
        "momentum": x.get("momentum") in ("↑", "↑↑"),
        "price_muted": abs(fnum(x.get("1D%"), 999)) <= 6.0,
        "rocket": bool(x.get("Rocket")),
    }


def select_watchlist(snapshot):
    # Union preserves strong candidates outside TOP10 if they are qualified rockets/triggers.
    merged = {}
    for bucket in (snapshot.get("top10") or [], snapshot.get("qualified_rockets") or [], snapshot.get("triggers") or []):
        for x in bucket:
            coin = x.get("coin")
            if coin:
                merged[coin] = x

    selected = []
    for coin, x in merged.items():
        flags = watch_flags(x)
        flag_count = sum(bool(v) for v in flags.values())
        vpd = fnum(x.get("VPD"))
        # Broad enough for POC, but requires a meaningful VPD seed.
        if vpd >= 75 or (vpd >= 60 and flag_count >= 4):
            selected.append({
                "coin": coin,
                "market": x.get("market") or f"KRW-{coin}",
                "VPD": vpd,
                "momentum": x.get("momentum"),
                "VPDVelocity": x.get("VPDVelocity"),
                "VPDVelocityPerHour": x.get("VPDVelocityPerHour"),
                "TodayValue/10": x.get("TodayValue/10"),
                "IntraAccel": x.get("IntraAccel"),
                "1D%": x.get("1D%"),
                "flags": [k for k, v in flags.items() if v],
                "flag_count": flag_count,
            })

    selected.sort(key=lambda z: (z["VPD"], fnum(z.get("VPDVelocityPerHour"), -999)), reverse=True)
    return selected[:MAX_WATCH]


def get_minute_candles(market, count=90):
    url = f"{BASE_URL}/candles/minutes/1"
    for attempt in range(3):
        try:
            r = SESSION.get(url, params={"market": market, "count": count}, timeout=12)
            if r.status_code == 200:
                rows = r.json()
                rows.sort(key=lambda x: x["candle_date_time_kst"])
                # Exclude the currently forming minute only when it is clearly current.
                now_key = datetime.now(KST).strftime("%Y-%m-%dT%H:%M")
                if rows and str(rows[-1].get("candle_date_time_kst", ""))[:16] == now_key:
                    rows = rows[:-1]
                return rows
            if r.status_code == 429:
                time.sleep(1 + attempt)
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return None


def block_sum(values, start_from_end, width):
    end = len(values) - start_from_end
    start = end - width
    if start < 0 or end <= start:
        return None
    return sum(values[start:end])


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def ratio(a, b):
    if a is None or b is None or b <= 0:
        return None
    return a / b


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1) * 100.0


def analyse_intraday(item):
    rows = get_minute_candles(item["market"], 90)
    if not rows or len(rows) < 65:
        return None
    values = [fnum(x.get("candle_acc_trade_price")) for x in rows]
    closes = [fnum(x.get("trade_price")) for x in rows]

    recent5 = block_sum(values, 0, 5)
    prev5_blocks = [block_sum(values, 5 * i, 5) for i in range(1, 7)]
    recent15 = block_sum(values, 0, 15)
    prev15_blocks = [block_sum(values, 15 * i, 15) for i in range(1, 4)]

    accel5 = ratio(recent5, mean(prev5_blocks))
    accel15 = ratio(recent15, mean(prev15_blocks))
    price5 = pct(closes[-1], closes[-6]) if len(closes) >= 6 else None
    price15 = pct(closes[-1], closes[-16]) if len(closes) >= 16 else None

    # POC Early Warning score. This is intentionally independent of VPD.
    score = 0
    a5 = fnum(accel5)
    a15 = fnum(accel15)
    p15 = abs(fnum(price15, 999))
    vpd = fnum(item.get("VPD"))

    if a5 >= 4.0: score += 30
    elif a5 >= 2.5: score += 24
    elif a5 >= 1.5: score += 15
    elif a5 >= 1.1: score += 8

    if a15 >= 3.0: score += 25
    elif a15 >= 2.0: score += 20
    elif a15 >= 1.4: score += 12
    elif a15 >= 1.1: score += 6

    if p15 <= 2.0: score += 20
    elif p15 <= 4.0: score += 12
    elif p15 <= 6.0: score += 5

    if vpd >= 85: score += 15
    elif vpd >= 75: score += 10
    elif vpd >= 65: score += 5

    if item.get("momentum") in ("↑", "↑↑"):
        score += 5
    if fnum(item.get("VPDVelocityPerHour"), -999) >= 0.75:
        score += 5

    # Level requires both score and real short-horizon turnover expansion.
    if score >= 75 and a5 >= 2.0 and a15 >= 1.4 and p15 <= 6.0:
        level = "RED"
    elif score >= 60 and a5 >= 1.5 and a15 >= 1.1 and p15 <= 8.0:
        level = "ORANGE"
    elif score >= 45:
        level = "WATCH"
    else:
        level = "QUIET"

    return {
        **item,
        "EWScore": score,
        "EWLevel": level,
        "ValueAccel5m": round(accel5, 2) if accel5 is not None else None,
        "ValueAccel15m": round(accel15, 2) if accel15 is not None else None,
        "Price5m%": round(price5, 2) if price5 is not None else None,
        "Price15m%": round(price15, 2) if price15 is not None else None,
        "Recent5mValueKRW": round(recent5, 0) if recent5 is not None else None,
        "Recent15mValueKRW": round(recent15, 0) if recent15 is not None else None,
    }


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"Telegram send failed: {r.status_code} {r.text[:300]}")
    print("Telegram message_id:", r.json()["result"]["message_id"])


def level_rank(level):
    return {"QUIET": 0, "WATCH": 1, "ORANGE": 2, "RED": 3}.get(level, 0)


def format_alert(x, cleared=False):
    now = datetime.now(KST).strftime("%H:%M")
    if cleared:
        return (
            f"🟢 VPD EARLY WARNING CLEAR | {x['coin']} | {now}\n"
            f"현재 {x['EWLevel']} | EW {x['EWScore']}\n"
            f"5m ValueAccel {x['ValueAccel5m']}x | 15m {x['ValueAccel15m']}x\n"
            f"Price 5m {x['Price5m%']}% | 15m {x['Price15m%']}%"
        )
    emoji = "🔴" if x["EWLevel"] == "RED" else "🟠"
    phrase = "수급 선행 / 가격 미반응 후보" if abs(fnum(x.get("Price15m%"), 999)) <= 4 else "수급 가속 후보"
    return (
        f"{emoji} VPD EARLY WARNING | {x['coin']} | {now}\n"
        f"{x['EWLevel']} {x['EWScore']} | VPD {x['VPD']:.0f} | Mom {x.get('momentum','-')}\n"
        f"5m ValueAccel {x['ValueAccel5m']}x | 15m {x['ValueAccel15m']}x\n"
        f"Price 5m {x['Price5m%']}% | 15m {x['Price15m%']}%\n"
        f"판정: {phrase}"
    )


def append_history(existing_text, events):
    fields = [
        "event_time", "coin", "market", "event", "EWLevel", "EWScore", "VPD", "momentum",
        "ValueAccel5m", "ValueAccel15m", "Price5m%", "Price15m%", "VPDVelocityPerHour",
    ]
    existing_rows = []
    if existing_text:
        try:
            existing_rows = list(csv.DictReader(io.StringIO(existing_text)))
        except Exception:
            existing_rows = []
    rows = existing_rows + events
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields)
    w.writeheader()
    for row in rows:
        w.writerow({k: row.get(k) for k in fields})
    return out.getvalue()


def main():
    snapshot = read_public_json("vpd_latest.json")
    if not snapshot:
        raise RuntimeError("vpd_latest.json unavailable")
    snap_time = parse_asof(snapshot.get("asof") or snapshot.get("asof_kst"))
    if not snap_time:
        raise RuntimeError("Invalid VPD snapshot timestamp")
    age_h = (datetime.now(KST) - snap_time).total_seconds() / 3600
    if age_h > MAX_SNAPSHOT_AGE_HOURS:
        print(f"Snapshot stale: {age_h:.2f}h > {MAX_SNAPSHOT_AGE_HOURS}h. Skip.")
        return

    watchlist = select_watchlist(snapshot)
    watch_doc = {
        "generated_at": datetime.now(KST).isoformat(),
        "vpd_asof": snapshot.get("asof_kst") or snapshot.get("asof"),
        "rule": "POC only; separate from VPD v1.4",
        "coins": watchlist,
    }
    # Candidate list changes mainly when the twice-daily VPD snapshot changes.
    github_put_text(WATCHLIST_PATH, json.dumps(watch_doc, ensure_ascii=False, indent=2), "Update VPD Early Warning watchlist")

    print("WATCHLIST:", ", ".join(x["coin"] for x in watchlist) or "none")
    observations = []
    for item in watchlist:
        obs = analyse_intraday(item)
        if obs:
            observations.append(obs)
            print(obs["coin"], obs["EWLevel"], obs["EWScore"], obs["ValueAccel5m"], obs["ValueAccel15m"], obs["Price15m%"])
        time.sleep(API_SLEEP)

    state_doc = read_public_json("early_warning_state.json", default={}) or {}
    state = state_doc.get("coins", {}) if isinstance(state_doc, dict) else {}
    events = []
    state_changed = False
    now_iso = datetime.now(KST).isoformat()

    observed_coins = {x["coin"] for x in observations}
    for x in observations:
        coin = x["coin"]
        current = x["EWLevel"]
        previous = (state.get(coin) or {}).get("level", "QUIET")

        should_alert = current in ("ORANGE", "RED") and level_rank(current) > level_rank(previous)
        should_clear = previous in ("ORANGE", "RED") and current in ("WATCH", "QUIET")

        if should_alert:
            send_telegram(format_alert(x))
            state[coin] = {"level": current, "updated_at": now_iso, "EWScore": x["EWScore"]}
            state_changed = True
            event_name = "ENTER_" + current if previous not in ("ORANGE", "RED") else f"{previous}_TO_{current}"
            events.append({
                "event_time": now_iso, "coin": coin, "market": x["market"], "event": event_name,
                "EWLevel": current, "EWScore": x["EWScore"], "VPD": x["VPD"], "momentum": x.get("momentum"),
                "ValueAccel5m": x.get("ValueAccel5m"), "ValueAccel15m": x.get("ValueAccel15m"),
                "Price5m%": x.get("Price5m%"), "Price15m%": x.get("Price15m%"),
                "VPDVelocityPerHour": x.get("VPDVelocityPerHour"),
            })
        elif should_clear:
            send_telegram(format_alert(x, cleared=True))
            state[coin] = {"level": current, "updated_at": now_iso, "EWScore": x["EWScore"]}
            state_changed = True
            events.append({
                "event_time": now_iso, "coin": coin, "market": x["market"], "event": "CLEAR",
                "EWLevel": current, "EWScore": x["EWScore"], "VPD": x["VPD"], "momentum": x.get("momentum"),
                "ValueAccel5m": x.get("ValueAccel5m"), "ValueAccel15m": x.get("ValueAccel15m"),
                "Price5m%": x.get("Price5m%"), "Price15m%": x.get("Price15m%"),
                "VPDVelocityPerHour": x.get("VPDVelocityPerHour"),
            })

    # If an alerted coin falls out of the current VPD watchlist, clear its persisted alert state silently.
    for coin, old in list(state.items()):
        if coin not in observed_coins and old.get("level") in ("ORANGE", "RED"):
            state[coin] = {"level": "QUIET", "updated_at": now_iso, "reason": "OUT_OF_WATCHLIST"}
            state_changed = True
            events.append({"event_time": now_iso, "coin": coin, "market": f"KRW-{coin}", "event": "OUT_OF_WATCHLIST_CLEAR", "EWLevel": "QUIET"})

    if state_changed:
        state_out = {"updated_at": now_iso, "coins": state}
        github_put_text(STATE_PATH, json.dumps(state_out, ensure_ascii=False, indent=2), "Update Early Warning alert state")

    if events:
        old_hist = read_public_text("early_warning_history.csv")
        hist = append_history(old_hist, events)
        github_put_text(HISTORY_PATH, hist, "Append Early Warning events")
    else:
        print("No alert transition; no state/history commit.")


if __name__ == "__main__":
    main()
