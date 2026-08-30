"""WPI5 Shadow 2.0 - 30-day historical replay for current VPD TOP10.

Purpose
- Rebuild an end-of-day analogue of VPD v1.5 from Upbit KRW daily OHLCV only.
- Test WPI5 as a FILTER together with replay VPD and maximum daily price reaction.
- Add volume-weighted WPI5 as an experimental comparison.

Important limitation
- Historical intraday 1H momentum at the original scan time is not reconstructed here.
- `MomentumCore` therefore uses RSI direction + Williams direction + Vol3/10 only.
- `VPDReplay` itself matches the v1.5 daily-input scoring structure, including MaxPriceReturn,
  but represents completed-day replay rather than the original partial-day scan state.
"""

import json
import time
from pathlib import Path
import requests
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "data" / "vpd_latest.json"
OUT_JSON = ROOT / "data" / "wpi5_replay_30d_latest.json"
OUT_CSV = ROOT / "data" / "wpi5_replay_30d.csv"
UA = {"User-Agent": "VPD-WPI5-Replay/2.0"}


def candles(market, count=80):
    r = requests.get(
        "https://api.upbit.com/v1/candles/days",
        params={"market": market, "count": count},
        headers=UA,
        timeout=15,
    )
    r.raise_for_status()
    arr = r.json()
    arr.reverse()
    return pd.DataFrame(arr)


def safe_ratio(a, b):
    try:
        if pd.isna(a) or pd.isna(b) or float(b) == 0:
            return np.nan
        return float(a) / float(b)
    except Exception:
        return np.nan


def calc_rsi(close, period=14):
    close = pd.Series(close).astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def calc_williams(df, period=14):
    high = df["high_price"].astype(float).rolling(period).max()
    low = df["low_price"].astype(float).rolling(period).min()
    close = df["trade_price"].astype(float)
    den = (high - low).replace(0, np.nan)
    return -100 * (high - close) / den


def score_vpd(vol3_10, today_value_10, intra_accel, rsi, williams, max_price_return, spike_collapse):
    # Exact v1.4 weights; v1.5 only changes price input to MaxPriceReturn.
    score = 0.0
    if not pd.isna(vol3_10):
        if vol3_10 >= 3.0: score += 25
        elif vol3_10 >= 2.0: score += 21
        elif vol3_10 >= 1.5: score += 17
        elif vol3_10 >= 1.2: score += 12
        elif vol3_10 >= 1.0: score += 7
    if not pd.isna(today_value_10):
        if today_value_10 >= 3.0: score += 20
        elif today_value_10 >= 2.0: score += 17
        elif today_value_10 >= 1.2: score += 13
        elif today_value_10 >= 0.8: score += 9
        elif today_value_10 >= 0.4: score += 5
    if not pd.isna(intra_accel):
        if intra_accel >= 4: score += 15
        elif intra_accel >= 2.5: score += 12
        elif intra_accel >= 1.5: score += 9
        elif intra_accel >= 1.0: score += 5
    if not pd.isna(rsi):
        if 45 <= rsi <= 65: score += 15
        elif 35 <= rsi < 45: score += 12
        elif 65 < rsi <= 72: score += 10
        elif 30 <= rsi < 35: score += 8
        elif 72 < rsi <= 78: score += 5
    if not pd.isna(williams):
        if -80 <= williams <= -40: score += 10
        elif -40 < williams <= -20: score += 8
        elif -90 <= williams < -80: score += 7
        elif -20 < williams <= -10: score += 4
    if not pd.isna(max_price_return):
        abs_r = abs(max_price_return)
        if abs_r <= 2: score += 15
        elif abs_r <= 4: score += 13
        elif abs_r <= 6: score += 10
        elif abs_r <= 10: score += 6
        elif abs_r <= 15: score += 2
    if spike_collapse:
        score -= 15
    return round(max(0, min(100, score)), 1)


def wick_metrics(frame):
    lows, ups, values = [], [], []
    for _, r in frame.iterrows():
        o = float(r.opening_price); h = float(r.high_price); l = float(r.low_price); c = float(r.trade_price)
        rng = max(h - l, 0.0)
        if rng <= 0:
            lo = up = 0.0
        else:
            lo = max(min(o, c) - l, 0.0) / rng
            up = max(h - max(o, c), 0.0) / rng
        lows.append(lo); ups.append(up); values.append(max(float(r.candle_acc_trade_price), 0.0))

    sl = float(sum(lows)); su = float(sum(ups)); den = sl + su
    wpi = 0.0 if den == 0 else (sl - su) / den * 100.0

    wlo = float(sum(lo * v for lo, v in zip(lows, values)))
    wup = float(sum(up * v for up, v in zip(ups, values)))
    wden = wlo + wup
    vwpi = 0.0 if wden == 0 else (wlo - wup) / wden * 100.0

    ld = sum(1 for lo, up in zip(lows, ups) if lo > up)
    ud = sum(1 for lo, up in zip(lows, ups) if up > lo)
    return {
        "WPI5": round(wpi, 2),
        "VWPI5": round(vwpi, 2),
        "LowerDays5": int(ld),
        "UpperDays5": int(ud),
        "LowerWickTotal5": round(sl, 4),
        "UpperWickTotal5": round(su, 4),
    }


def momentum_core(rsi_now, rsi_prev, will_now, will_prev, vol3_10):
    # Deliberately excludes historical 1H return because exact original scan-time 1H is unavailable.
    points = 0
    if not pd.isna(rsi_now) and not pd.isna(rsi_prev):
        d = rsi_now - rsi_prev
        if d >= 5: points += 2
        elif d > 0: points += 1
    if not pd.isna(will_now) and not pd.isna(will_prev):
        d = will_now - will_prev
        if d >= 15: points += 2
        elif d > 0: points += 1
    if not pd.isna(vol3_10):
        if vol3_10 >= 2: points += 2
        elif vol3_10 >= 1.2: points += 1
    if points >= 5: return "↑↑"
    if points >= 3: return "↑"
    if points >= 1: return "→"
    return "↓"


def forward_stats(df, i):
    base = float(df.iloc[i].trade_price)
    out = {}
    for n in (1, 3, 5):
        out[f"D{n}%"] = round((float(df.iloc[i+n].trade_price) / base - 1) * 100, 2) if i+n < len(df) else None
    future = df.iloc[i+1:min(i+6, len(df))]
    if len(future):
        out["MFE5%"] = round((future.high_price.astype(float).max() / base - 1) * 100, 2)
        out["MAE5%"] = round((future.low_price.astype(float).min() / base - 1) * 100, 2)
    else:
        out["MFE5%"] = None; out["MAE5%"] = None
    return out


def perf(g):
    s = {"N": int(len(g))}
    for col in ("D1%", "D3%", "D5%", "MFE5%", "MAE5%"):
        z = pd.to_numeric(g[col], errors="coerce").dropna()
        s["Avg" + col] = round(float(z.mean()), 2) if len(z) else None
        s["Median" + col] = round(float(z.median()), 2) if len(z) else None
    for n in (1, 3, 5):
        z = pd.to_numeric(g[f"D{n}%"], errors="coerce").dropna()
        s[f"D{n}WinRate%"] = round(float((z > 0).mean() * 100), 1) if len(z) else None
    return s


def main():
    latest = json.loads(LATEST.read_text(encoding="utf-8"))
    top = latest.get("top10", [])[:10]
    rows = []

    for x in top:
        market = x["market"]; coin = x["coin"]
        df = candles(market, 80)
        # newest candle is current partial day; historical replay uses completed candles only.
        completed = df.iloc[:-1].copy().reset_index(drop=True)
        completed["RSI14"] = calc_rsi(completed["trade_price"])
        completed["Williams"] = calc_williams(completed)
        volume = completed["candle_acc_trade_volume"].astype(float)
        value = completed["candle_acc_trade_price"].astype(float)

        # Last ~30 signal days, leaving five future days where possible for D+5/MFE/MAE.
        start = max(20, len(completed) - 35)
        end = len(completed)
        for i in range(start, end):
            if i < 14 or i < 10 or i < 5:
                continue
            prev_close = float(completed.iloc[i-1].trade_price)
            high = float(completed.iloc[i].high_price)
            close = float(completed.iloc[i].trade_price)
            maxret = (high / prev_close - 1) * 100 if prev_close else np.nan
            dayret = (close / prev_close - 1) * 100 if prev_close else np.nan

            vol3 = volume.iloc[i-2:i+1].mean()
            vol10 = volume.iloc[i-10:i].mean()
            vol3_10 = safe_ratio(vol3, vol10)
            today_value_10 = safe_ratio(value.iloc[i], value.iloc[i-10:i].mean())
            intra_accel = safe_ratio(value.iloc[i], value.iloc[i-3:i].mean())

            previous_values = value.iloc[i-10:i]
            median_val = previous_values.median(); max_val = previous_values.max()
            spike = False
            if median_val > 0 and max_val > 0:
                spike = bool((max_val / median_val) >= 5 and (value.iloc[i] / max_val) <= 0.25)

            rsi_now = float(completed.iloc[i].RSI14) if not pd.isna(completed.iloc[i].RSI14) else np.nan
            rsi_prev = float(completed.iloc[i-1].RSI14) if not pd.isna(completed.iloc[i-1].RSI14) else np.nan
            will_now = float(completed.iloc[i].Williams) if not pd.isna(completed.iloc[i].Williams) else np.nan
            will_prev = float(completed.iloc[i-1].Williams) if not pd.isna(completed.iloc[i-1].Williams) else np.nan
            vpd = score_vpd(vol3_10, today_value_10, intra_accel, rsi_now, will_now, maxret, spike)
            wm = wick_metrics(completed.iloc[i-5:i])

            row = {
                "coin": coin,
                "date": str(completed.iloc[i].candle_date_time_kst)[:10],
                "VPDReplay": vpd,
                "MomentumCore": momentum_core(rsi_now, rsi_prev, will_now, will_prev, vol3_10),
                "MaxPriceReturn%": round(maxret, 2),
                "DayReturn%": round(dayret, 2),
                "Vol3/10": round(vol3_10, 2) if not pd.isna(vol3_10) else None,
                "TodayValue/10": round(today_value_10, 2) if not pd.isna(today_value_10) else None,
                "IntraAccel": round(intra_accel, 2) if not pd.isna(intra_accel) else None,
                **wm,
                **forward_stats(completed, i),
            }
            rows.append(row)
        time.sleep(0.08)

    rdf = pd.DataFrame(rows)
    if not len(rdf):
        raise RuntimeError("No historical replay rows")
    rdf.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # Fixed WPI bands: avoids choosing thresholds after seeing performance.
    bins = [(-999, -40, "WPI<-40"), (-40, -20, "-40<=WPI<-20"), (-20, 0, "-20<=WPI<0"),
            (0, 20, "0<=WPI<20"), (20, 40, "20<=WPI<40"), (40, 999, "WPI>=40")]
    wpi_bands = []
    for lo, hi, label in bins:
        g = rdf[(rdf.WPI5 >= lo) & (rdf.WPI5 < hi)]
        if len(g): wpi_bands.append({"group": label, **perf(g)})

    # Predefined combination tests focused on the original hypothesis:
    # strong flow + positive wick pressure + limited price reaction.
    tests = [
        ("BASE_ALL", pd.Series(True, index=rdf.index)),
        ("VPD65+", rdf.VPDReplay >= 65),
        ("VPD75+", rdf.VPDReplay >= 75),
        ("WPI20+", rdf.WPI5 >= 20),
        ("WPI40+", rdf.WPI5 >= 40),
        ("VPD65+_WPI20+", (rdf.VPDReplay >= 65) & (rdf.WPI5 >= 20)),
        ("VPD65+_WPI40+", (rdf.VPDReplay >= 65) & (rdf.WPI5 >= 40)),
        ("VPD65+_MAX6_WPI20+", (rdf.VPDReplay >= 65) & (rdf["MaxPriceReturn%"] <= 6) & (rdf.WPI5 >= 20)),
        ("VPD65+_MAX10_WPI20+", (rdf.VPDReplay >= 65) & (rdf["MaxPriceReturn%"] <= 10) & (rdf.WPI5 >= 20)),
        ("VPD75+_MAX6_WPI20+", (rdf.VPDReplay >= 75) & (rdf["MaxPriceReturn%"] <= 6) & (rdf.WPI5 >= 20)),
        ("VPD75+_MAX10_WPI20+", (rdf.VPDReplay >= 75) & (rdf["MaxPriceReturn%"] <= 10) & (rdf.WPI5 >= 20)),
        ("VPD65+_MAX6_VWPI20+", (rdf.VPDReplay >= 65) & (rdf["MaxPriceReturn%"] <= 6) & (rdf.VWPI5 >= 20)),
        ("VPD65+_MAX10_VWPI20+", (rdf.VPDReplay >= 65) & (rdf["MaxPriceReturn%"] <= 10) & (rdf.VWPI5 >= 20)),
        ("VPD65+_MAX10_WPI20+_LOWER4+", (rdf.VPDReplay >= 65) & (rdf["MaxPriceReturn%"] <= 10) & (rdf.WPI5 >= 20) & (rdf.LowerDays5 >= 4)),
    ]
    combos = []
    for name, mask in tests:
        g = rdf[mask]
        combos.append({"group": name, **perf(g)})

    # Per-coin summary for the key latent-price pattern.
    keymask = (rdf.VPDReplay >= 65) & (rdf["MaxPriceReturn%"] <= 10) & (rdf.WPI5 >= 20)
    by_coin = []
    for coin, g in rdf[keymask].groupby("coin"):
        by_coin.append({"coin": coin, **perf(g)})
    by_coin.sort(key=lambda x: x.get("N", 0), reverse=True)

    doc = {
        "name": "WPI5 Shadow 2.0 Historical Replay",
        "scope": "Current VPD TOP10, ~30 completed daily signal dates per coin",
        "source": "Upbit KRW daily OHLCV only",
        "rows": int(len(rdf)),
        "limitations": [
            "End-of-day replay; not an exact reconstruction of historical intraday scanner snapshots.",
            "MomentumCore excludes historical 1H return; do not compare it directly with live Momentum labels.",
            "Current TOP10 membership creates selection bias; this is a fast POC, not full-universe proof."
        ],
        "wpi_bands": wpi_bands,
        "combination_tests": combos,
        "key_combo_by_coin": by_coin,
    }
    OUT_JSON.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(doc, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
