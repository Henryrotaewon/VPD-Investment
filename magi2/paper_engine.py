"""MAGI2 Upbit PAPER monitor.

Daily Cohort v0.1 rules
- PAPER ONLY: never sends Upbit orders.
- Morning VPD TOP10 is selected from the 07:20 scan and entered at live price
  immediately after the scanner workflow completes.
- Entry uses configured slippage and buy fee; 300,000 KRW is total cash outlay.
- Returns/TP/SL use net liquidation value after estimated sell fee.
- Exit immediately at take-profit or hard-stop.
- Any remaining position is liquidated at/after 07:10 KST on the next day.
- Sends Telegram portfolio status every monitoring run and SELL IMMINENT alerts
  every minute while a position is inside the warning zone.
"""

import json
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "magi2/config.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "data/magi2_paper_state.json"
LOG_PATH = ROOT / "data/magi2_trade_log.jsonl"
VPD_PATH = ROOT / "data/vpd_latest.json"
KST = ZoneInfo("Asia/Seoul")


def now_dt(): return datetime.now(KST)
def now_iso(): return now_dt().isoformat()
def parse_hhmm(text):
    hh, mm = map(int, text.split(":")); return dtime(hh, mm)


def log_event(event):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_state():
    if not STATE_PATH.exists():
        return {"mode":"PAPER","initial_cash_krw":float(CFG["initial_cash_krw"]),"cash_krw":float(CFG["initial_cash_krw"]),"positions":{},"realized_pnl_krw":0.0,"lifetime_realized_pnl_krw":0.0}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(st):
    st["updated_at"] = now_iso()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def http_json(url, timeout=10):
    req = Request(url, headers={"User-Agent":"MAGI2-Paper/0.1"})
    with urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode("utf-8"))


def get_prices(markets):
    if not markets: return {}
    rows = http_json("https://api.upbit.com/v1/ticker?" + urlencode({"markets": ",".join(markets)}))
    return {row["market"]: float(row["trade_price"]) for row in rows}


def telegram(text):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[Telegram disabled]", text); return
    data = urlencode({"chat_id":chat_id,"text":text}).encode("utf-8")
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, headers={"Content-Type":"application/x-www-form-urlencoded"}, method="POST")
    with urlopen(req, timeout=10) as r: r.read()


def position_return(p, px):
    """Net return if liquidated now: includes buy fee already in cost and estimated sell fee."""
    fee_rate = float(CFG.get("fee_rate", 0.0005))
    net_value = float(p["qty"]) * px * (1.0 - fee_rate)
    return (net_value / float(p["cost_krw"]) - 1.0) * 100.0


def migrate_buy_fee(st):
    """One-time migration of legacy paper positions that used slippage but omitted buy fee."""
    fee_rate = float(CFG.get("fee_rate", 0.0005))
    changed = False
    for p in st.get("positions", {}).values():
        if "buy_fee_krw" in p: continue
        cost = float(p["cost_krw"])
        notional = cost / (1.0 + fee_rate)
        buy_fee = cost - notional
        p["buy_notional_krw"] = notional
        p["buy_fee_krw"] = buy_fee
        p["qty"] = notional / float(p["entry_price"])
        changed = True
        if p.get("status") == "CLOSED" and p.get("exit_price") is not None:
            gross = p["qty"] * float(p["exit_price"])
            sell_fee = gross * fee_rate
            proceeds = gross - sell_fee
            pnl = proceeds - cost
            p["sell_fee_krw"] = sell_fee
            p["pnl_krw"] = round(pnl, 2)
            p["return_pct"] = round(pnl / cost * 100.0, 4)
    if changed:
        open_cost = sum(float(p["cost_krw"]) for p in st.get("positions", {}).values() if p.get("status","OPEN") == "OPEN")
        closed = [p for p in st.get("positions", {}).values() if p.get("status") == "CLOSED"]
        closed_proceeds = sum(float(p["cost_krw"]) + float(p.get("pnl_krw",0)) for p in closed)
        principal = float(st.get("initial_cash_krw", CFG["initial_cash_krw"]))
        st["cash_krw"] = principal - open_cost - sum(float(p["cost_krw"]) for p in closed) + closed_proceeds
        st["realized_pnl_krw"] = sum(float(p.get("pnl_krw",0)) for p in closed)
        st["cost_model"] = "SLIPPAGE_BUY_FEE_SELL_FEE_NET"
        save_state(st)
    return changed


def portfolio_equity(st, prices):
    fee_rate = float(CFG.get("fee_rate",0.0005)); equity = float(st.get("cash_krw",0.0))
    for p in st.get("positions",{}).values():
        if p.get("status","OPEN") != "OPEN": continue
        px = prices.get(p["market"], float(p.get("last_price",p["entry_price"])))
        equity += float(p["qty"]) * px * (1.0-fee_rate)
    principal = float(st.get("initial_cash_krw",CFG["initial_cash_krw"])); pnl = equity-principal
    return principal,equity,pnl,(pnl/principal*100.0 if principal else 0.0)


def portfolio_status(st, prices, title="📊 MAGI2 PAPER 매매현황"):
    active=[(c,p) for c,p in st.get("positions",{}).items() if p.get("status","OPEN")=="OPEN"]
    principal,equity,total_pnl,total_ret=portfolio_equity(st,prices)
    lines=[f"📅 {now_dt().strftime('%Y-%m-%d')} | 원금 {principal:,.0f}원 | 평가 {equity:,.0f}원 | {total_ret:+.2f}% ({total_pnl:+,.0f}원)",title,now_dt().strftime("%Y-%m-%d %H:%M KST"),f"Cohort: {st.get('cohort_id','-')}"]
    for coin,p in active:
        px=prices.get(p["market"],float(p.get("last_price",p["entry_price"]))); ret=position_return(p,px)
        lines.append(f"{coin} / {int(p['cost_krw']):,}원 / {ret:+.2f}% / 목표 +{float(p['target_profit_pct']):.1f}%")
    if not active: lines.append("현재 보유 포지션 없음")
    lines += [f"금일 실현손익: {float(st.get('realized_pnl_krw',0)):+,.0f}원","※ 수익률=슬리피지+매수/매도 수수료 반영 · PAPER ONLY"]
    return "\n".join(lines)


def close_position(st, coin, p, px, reason):
    fee_rate=float(CFG.get("fee_rate",0.0005)); gross=float(p["qty"])*px; fee=gross*fee_rate; proceeds=gross-fee
    pnl=proceeds-float(p["cost_krw"]); ret=pnl/float(p["cost_krw"])*100.0
    p.update({"status":"CLOSED","exit_at":now_iso(),"exit_price":px,"exit_reason":reason,"sell_fee_krw":fee,"return_pct":round(ret,4),"pnl_krw":round(pnl,2)})
    st["cash_krw"]=float(st.get("cash_krw",0))+proceeds; st["realized_pnl_krw"]=float(st.get("realized_pnl_krw",0))+pnl; st["lifetime_realized_pnl_krw"]=float(st.get("lifetime_realized_pnl_krw",0))+pnl
    log_event({"ts":now_iso(),"type":"SELL","cohort_id":st.get("cohort_id"),"coin":coin,"reason":reason,"price":px,"sell_fee_krw":round(fee,2),"return_pct":round(ret,2),"pnl_krw":round(pnl,2),"paper_only":True})
    telegram("✅ MAGI2 PAPER 매도완료\n"+f"{coin} / 매수금액 {int(p['cost_krw']):,}원 / 순수익률 {ret:+.2f}%\n"+f"목표수익률 설정값 +{float(p['target_profit_pct']):.1f}% / 사유 {reason}\n"+"※ 슬리피지+매수/매도 수수료 반영 · PAPER ONLY")


def maybe_time_exit(st):
    exit_t=parse_hhmm(CFG.get("session",{}).get("time_exit_kst","07:10")); now=now_dt(); text=st.get("cohort_date")
    if not text or now.time()<exit_t: return False
    try: cohort_date=datetime.fromisoformat(text).date()
    except ValueError: return False
    if cohort_date>=now.date(): return False
    active={c:p for c,p in st.get("positions",{}).items() if p.get("status","OPEN")=="OPEN"}
    if not active: return False
    prices=get_prices([p["market"] for p in active.values()])
    for coin,p in list(active.items()):
        px=prices.get(p["market"])
        if px is not None: close_position(st,coin,p,px,"TIME_EXIT_0710")
    save_state(st); telegram(portfolio_status(st,prices,title="⏰ MAGI2 07:10 일괄청산 완료")); return True


def load_morning_snapshot():
    if not VPD_PATH.exists(): return None
    snap=json.loads(VPD_PATH.read_text(encoding="utf-8")); raw=snap.get("asof")
    if not raw: return None
    asof=datetime.fromisoformat(raw).astimezone(KST); session=CFG.get("session",{}); start=parse_hhmm(session.get("entry_after_kst","07:20")); end=parse_hhmm(session.get("entry_window_end_kst","08:00"))
    if asof.date()!=now_dt().date() or not(start<=asof.time()<=end): return None
    return snap,asof


def maybe_initialize_daily_cohort(st):
    loaded=load_morning_snapshot()
    if loaded is None: return False
    snap,asof=loaded; today=asof.date().isoformat()
    if st.get("cohort_date")==today or any(p.get("status","OPEN")=="OPEN" for p in st.get("positions",{}).values()): return False
    candidates=snap.get("top10",[])[:int(CFG.get("session",{}).get("top_n",10))]
    if not candidates: return False
    prices=get_prices([x["market"] for x in candidates]); principal=float(CFG["initial_cash_krw"]); budget=float(CFG["position_krw"]); slippage=float(CFG.get("slippage_rate",0.001)); fee_rate=float(CFG.get("fee_rate",0.0005)); tp=float(CFG["exit"]["take_profit_pct"]); sl=float(CFG["exit"]["hard_stop_pct"]); warning=float(CFG["exit"]["warning_profit_pct"]); lifetime=float(st.get("lifetime_realized_pnl_krw",0.0))
    st.clear(); st.update({"mode":"PAPER","cohort_id":f"{today}-AM-001","cohort_date":today,"strategy":CFG.get("paper_strategy","VPD_TOP10_EQUAL_WEIGHT"),"source_snapshot_asof_kst":snap.get("asof_kst",asof.isoformat()),"initial_cash_krw":principal,"cash_krw":principal,"positions":{},"realized_pnl_krw":0.0,"lifetime_realized_pnl_krw":lifetime,"cost_model":"SLIPPAGE_BUY_FEE_SELL_FEE_NET","paper_only":True})
    for row in candidates:
        coin,market=row["coin"],row["market"]; live_px=prices.get(market)
        if live_px is None or st["cash_krw"]<budget: continue
        fill=live_px*(1.0+slippage); buy_notional=budget/(1.0+fee_rate); buy_fee=budget-buy_notional; qty=buy_notional/fill
        st["positions"][coin]={"market":market,"status":"OPEN","entry_at":now_iso(),"signal_rank":row.get("Rank"),"signal_vpd":row.get("VPD"),"signal_price":row.get("price"),"entry_market_price":live_px,"entry_price":fill,"buy_notional_krw":buy_notional,"buy_fee_krw":buy_fee,"qty":qty,"cost_krw":budget,"target_profit_pct":tp,"stop_loss_pct":sl,"warning_profit_pct":warning,"last_price":live_px,"peak_price":live_px}
        st["cash_krw"]-=budget
        log_event({"ts":now_iso(),"type":"BUY","cohort_id":st["cohort_id"],"coin":coin,"market":market,"budget_krw":budget,"buy_notional_krw":round(buy_notional,2),"buy_fee_krw":round(buy_fee,2),"market_price":live_px,"fill_price":fill,"target_profit_pct":tp,"stop_loss_pct":sl,"signal_rank":row.get("Rank"),"signal_vpd":row.get("VPD"),"paper_only":True})
    save_state(st); telegram("🟢 MAGI2 DAILY COHORT 매수완료\n"+f"{st['cohort_id']} / {len(st['positions'])}종목 / 종목당 총투입 {budget:,.0f}원\n"+f"TP +{tp:.1f}% / SL {sl:.1f}% / 익일 07:10 미도달분 일괄청산\n"+f"슬리피지 {slippage*100:.2f}% + 매수/매도 수수료 각 {fee_rate*100:.2f}% 반영\n※ PAPER ONLY · 실주문 없음"); return True


def monitor_once(st,send_status=False):
    active={c:p for c,p in st.get("positions",{}).items() if p.get("status","OPEN")=="OPEN"}; prices=get_prices([p["market"] for p in active.values()])
    if send_status: telegram(portfolio_status(st,prices))
    tp_default=float(CFG["exit"]["take_profit_pct"]); sl_default=float(CFG["exit"]["hard_stop_pct"]); warning_default=float(CFG["exit"]["warning_profit_pct"])
    for coin,p in list(active.items()):
        px=prices.get(p["market"])
        if px is None: continue
        p["last_price"]=px; p["peak_price"]=max(float(p.get("peak_price",p["entry_price"])),px); ret=position_return(p,px); tp=float(p.get("target_profit_pct",tp_default)); sl=float(p.get("stop_loss_pct",sl_default)); warning=float(p.get("warning_profit_pct",warning_default))
        if ret>=tp: close_position(st,coin,p,px,"TAKE_PROFIT")
        elif ret<=sl: close_position(st,coin,p,px,"HARD_STOP")
        elif ret>=warning: telegram("⚠️ MAGI2 PAPER 매도임박\n"+f"{coin} / 매수금액 {int(p['cost_krw']):,}원 / 순수익률 {ret:+.2f}%\n"+f"목표수익률 설정값 +{tp:.1f}% / 목표까지 {tp-ret:.2f}%p\n1분 단위 감시 중 · 비용 반영 · PAPER ONLY")
    save_state(st); return prices


def main():
    st=load_state()
    if st.get("mode")!="PAPER": raise RuntimeError("MAGI2 state is not PAPER mode")
    migrate_buy_fee(st); maybe_time_exit(st); maybe_initialize_daily_cohort(st)
    poll_seconds=int(CFG.get("monitor",{}).get("near_target_poll_seconds",60)); run_minutes=int(CFG.get("monitor",{}).get("run_window_minutes",14)); deadline=time.time()+run_minutes*60
    monitor_once(st,send_status=True)
    while time.time()+poll_seconds<=deadline:
        time.sleep(poll_seconds); monitor_once(st,send_status=False)
    print(json.dumps({"mode":"PAPER","cohort_id":st.get("cohort_id"),"open_positions":sum(1 for p in st.get("positions",{}).values() if p.get("status","OPEN")=="OPEN"),"realized_pnl_krw":round(float(st.get("realized_pnl_krw",0)),2),"updated_at":st.get("updated_at")},ensure_ascii=False,indent=2))


if __name__=="__main__": main()
