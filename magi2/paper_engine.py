"""MAGI2 Upbit PAPER monitor.

- PAPER ONLY: never sends Upbit orders.
- Monitors the fixed VPD TOP10 cohort in data/magi2_paper_state.json.
- Sends Telegram portfolio status every workflow run (15-minute schedule).
- Polls Upbit prices every minute during the run window.
- Sends SELL IMMINENT every minute while a position is inside the warning zone.
- Simulates TP/SL exits and sends SELL COMPLETE.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "magi2/config.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "data/magi2_paper_state.json"
LOG_PATH = ROOT / "data/magi2_trade_log.jsonl"
KST = ZoneInfo("Asia/Seoul")


def now_dt():
    return datetime.now(KST)


def now_iso():
    return now_dt().isoformat()


def log_event(event):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_state():
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(st):
    st["updated_at"] = now_iso()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def http_json(url, timeout=10):
    req = Request(url, headers={"User-Agent": "MAGI2-Paper/0.1"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_prices(markets):
    if not markets:
        return {}
    q = urlencode({"markets": ",".join(markets)})
    rows = http_json("https://api.upbit.com/v1/ticker?" + q)
    return {row["market"]: float(row["trade_price"]) for row in rows}


def telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[Telegram disabled]", text)
        return
    data = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=10) as r:
        r.read()


def position_return(p, px):
    return (px / float(p["entry_price"]) - 1.0) * 100.0


def portfolio_status(st, prices, title="📊 MAGI2 PAPER 매매현황"):
    active = [(c, p) for c, p in st.get("positions", {}).items() if p.get("status", "OPEN") == "OPEN"]
    lines = [title, now_dt().strftime("%Y-%m-%d %H:%M KST"), f"Cohort: {st.get('cohort_id', '-')}"]
    if not active:
        lines.append("현재 보유 포지션 없음")
    else:
        for coin, p in active:
            px = prices.get(p["market"], float(p.get("last_price", p["entry_price"])))
            ret = position_return(p, px)
            lines.append(
                f"{coin} / {int(p['cost_krw']):,}원 / {ret:+.2f}% / 목표 +{float(p['target_profit_pct']):.1f}%"
            )
    lines.append(f"실현손익: {float(st.get('realized_pnl_krw', 0)):+,.0f}원")
    lines.append("※ PAPER ONLY · 실주문 없음")
    return "\n".join(lines)


def close_position(st, coin, p, px, reason):
    fee_rate = float(CFG.get("fee_rate", 0.0005))
    gross = float(p["qty"]) * px
    fee = gross * fee_rate
    proceeds = gross - fee
    pnl = proceeds - float(p["cost_krw"])
    ret = position_return(p, px)
    p.update({
        "status": "CLOSED",
        "exit_at": now_iso(),
        "exit_price": px,
        "exit_reason": reason,
        "return_pct": round(ret, 4),
        "pnl_krw": round(pnl, 2),
    })
    st["cash_krw"] = float(st.get("cash_krw", 0)) + proceeds
    st["realized_pnl_krw"] = float(st.get("realized_pnl_krw", 0)) + pnl
    event = {
        "ts": now_iso(), "type": "SELL", "cohort_id": st.get("cohort_id"),
        "coin": coin, "reason": reason, "price": px,
        "return_pct": round(ret, 2), "pnl_krw": round(pnl, 2),
    }
    log_event(event)
    telegram(
        "✅ MAGI2 PAPER 매도완료\n"
        f"{coin} / 매수금액 {int(p['cost_krw']):,}원 / 수익률 {ret:+.2f}%\n"
        f"목표수익률 설정값 +{float(p['target_profit_pct']):.1f}% / 사유 {reason}\n"
        "※ PAPER ONLY · 실주문 없음"
    )


def monitor_once(st, send_status=False):
    active = {c: p for c, p in st.get("positions", {}).items() if p.get("status", "OPEN") == "OPEN"}
    markets = [p["market"] for p in active.values()]
    prices = get_prices(markets)

    if send_status:
        telegram(portfolio_status(st, prices))

    tp_default = float(CFG["exit"]["take_profit_pct"])
    sl_default = float(CFG["exit"]["hard_stop_pct"])
    warning_default = float(CFG["exit"]["warning_profit_pct"])

    for coin, p in list(active.items()):
        px = prices.get(p["market"])
        if px is None:
            continue
        p["last_price"] = px
        p["peak_price"] = max(float(p.get("peak_price", p["entry_price"])), px)
        ret = position_return(p, px)
        tp = float(p.get("target_profit_pct", tp_default))
        sl = float(p.get("stop_loss_pct", sl_default))
        warning = float(p.get("warning_profit_pct", warning_default))

        if ret >= tp:
            close_position(st, coin, p, px, "TAKE_PROFIT")
        elif ret <= sl:
            close_position(st, coin, p, px, "HARD_STOP")
        elif ret >= warning:
            remaining = tp - ret
            telegram(
                "⚠️ MAGI2 PAPER 매도임박\n"
                f"{coin} / 매수금액 {int(p['cost_krw']):,}원 / 수익률 {ret:+.2f}%\n"
                f"목표수익률 설정값 +{tp:.1f}% / 목표까지 {remaining:.2f}%p\n"
                "1분 단위 감시 중 · PAPER ONLY"
            )

    save_state(st)
    return prices


def main():
    st = load_state()
    if st.get("mode") != "PAPER":
        raise RuntimeError("MAGI2 state is not PAPER mode")

    poll_seconds = int(CFG.get("monitor", {}).get("near_target_poll_seconds", 60))
    run_minutes = int(CFG.get("monitor", {}).get("run_window_minutes", 14))
    deadline = time.time() + run_minutes * 60

    # 15-minute status at the beginning of each scheduled job.
    monitor_once(st, send_status=True)

    # Keep watching once per minute until just before the next 15-minute job.
    while time.time() + poll_seconds <= deadline:
        time.sleep(poll_seconds)
        monitor_once(st, send_status=False)

    print(json.dumps({
        "mode": "PAPER",
        "cohort_id": st.get("cohort_id"),
        "open_positions": sum(1 for p in st.get("positions", {}).values() if p.get("status", "OPEN") == "OPEN"),
        "realized_pnl_krw": round(float(st.get("realized_pnl_krw", 0)), 2),
        "updated_at": st.get("updated_at"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
