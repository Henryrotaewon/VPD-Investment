import argparse, json, os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/'magi2'/'config.json').read_text(encoding='utf-8'))
STATE_PATH=ROOT/'magi2'/'state'/'paper_state.json'
EVENTS_PATH=ROOT/'magi2'/'state'/'paper_events.jsonl'
VPD_PATH=ROOT/'data'/'vpd_latest.json'
KST=ZoneInfo('Asia/Seoul')

def now_dt(): return datetime.now(KST)
def now_iso(): return now_dt().isoformat()

def telegram(text):
    token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat: return
    try: requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':text},timeout=15).raise_for_status()
    except Exception as e: print(f'Telegram error: {e}')

def save_state(st):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True); st['updated_at']=now_iso(); STATE_PATH.write_text(json.dumps(st,ensure_ascii=False,indent=2),encoding='utf-8')

def load_state():
    if not STATE_PATH.exists(): raise RuntimeError('MAGI2 paper_state.json is missing. Refusing automatic reset.')
    if CFG.get('mode')!='PAPER': raise RuntimeError('MAGI2 paper_engine.py may run only in PAPER mode.')
    return json.loads(STATE_PATH.read_text(encoding='utf-8'))

def log_event(e):
    EVENTS_PATH.parent.mkdir(parents=True,exist_ok=True)
    with EVENTS_PATH.open('a',encoding='utf-8') as f: f.write(json.dumps(e,ensure_ascii=False)+'\n')

def get_prices(markets):
    markets=list(dict.fromkeys(m for m in markets if m)); out={}
    for i in range(0,len(markets),100):
        try:
            r=requests.get('https://api.upbit.com/v1/ticker',params={'markets':','.join(markets[i:i+100])},timeout=15); r.raise_for_status()
            for x in r.json(): out[x['market']]=float(x['trade_price'])
        except Exception as e: print('price error',e)
    return out

def position_return(p,px):
    fee=float(CFG.get('fee_rate',.0005)); return (float(p['qty'])*float(px)*(1-fee)/float(p['cost_krw'])-1)*100

def portfolio_status(st,prices,title='📊 MAGI2 PAPER 현황'):
    fee=float(CFG.get('fee_rate',.0005)); cash=float(st.get('cash_krw',0)); value=0; rows=[]
    for coin,p in st.get('positions',{}).items():
        if p.get('status','OPEN')!='OPEN': continue
        px=prices.get(p['market'],float(p.get('last_price',p['entry_price']))); value+=float(p['qty'])*px*(1-fee)
        rows.append((coin,position_return(p,px),p.get('entry_session','AM'),float(p['cost_krw']),float(p.get('target_profit_pct',12)),float(p.get('stop_loss_pct',-6))))
    base=float(st.get('initial_cash_krw',CFG['initial_cash_krw'])); equity=cash+value; ret=(equity/base-1)*100 if base else 0; invested=sum(x[3] for x in rows)
    lines=[title,f"Cohort {st.get('cohort_id','-')}",f'최초원금 {base:,.0f}원',f'매수원금 합계 {invested:,.0f}원 / 예수금 {cash:,.0f}원',f'평가 {equity:,.0f}원 / 누적 {ret:+.2f}% ({equity-base:+,.0f}원)']
    slot=float(st.get('daily_equal_buy_krw',0) or 0)
    if slot>0: lines.append(f'당일 균등매수원가 {slot:,.0f}원')
    for c,r,s,cost,tp,sl in sorted(rows,key=lambda x:x[1],reverse=True):
        tag=' REFILL' if s=='REFILL' else (' PM' if s=='PM_REFILL' else '')
        lines.append(f'{c}{tag} | 원금 {cost:,.0f}원 | {r:+.2f}% | TP +{tp:.1f}% / SL {sl:.1f}%')
    lines += [f"금일 실현손익 {float(st.get('realized_pnl_krw',0)):+,.0f}원",'수익률=슬리피지+매수/매도 수수료 반영 · PAPER ONLY']
    return '\n'.join(lines)

def send_current_status(st,title='📊 MAGI2 PAPER 현황'):
    active=[p['market'] for p in st.get('positions',{}).values() if p.get('status','OPEN')=='OPEN']; telegram(portfolio_status(st,get_prices(active),title))

def migrate_state(st):
    fee=float(CFG.get('fee_rate',.0005)); changed=False
    for p in st.get('positions',{}).values():
        if 'buy_fee_krw' not in p:
            cost=float(p.get('cost_krw',CFG.get('position_krw',300000))); n=cost/(1+fee); p['buy_notional_krw']=n; p['buy_fee_krw']=cost-n
            if float(p.get('entry_price',0)): p['qty']=n/float(p['entry_price'])
            changed=True
        if 'entry_session' not in p: p['entry_session']='AM'; changed=True
    if changed: save_state(st)

def load_today_snapshot():
    repo=os.getenv('MAGI_GITHUB_REPO','Henryrotaewon/VPD-Investment').strip()
    url=f'https://raw.githubusercontent.com/{repo}/main/data/vpd_latest.json'
    try:
        r=requests.get(url,timeout=20); r.raise_for_status(); snap=r.json()
    except Exception as e:
        print(f'VPD GitHub fetch error: {e}'); return None
    raw=snap.get('asof') or snap.get('asof_kst')
    if not raw: return None
    try: asof=datetime.fromisoformat(raw).astimezone(KST)
    except ValueError: return None
    return (snap,asof) if asof.date()==now_dt().date() else None

def load_morning_snapshot():
    loaded=load_today_snapshot()
    if not loaded: return None
    snap,asof=loaded; s=CFG.get('session',{}); start=datetime.strptime(s.get('entry_after_kst','07:20'),'%H:%M').time(); end=datetime.strptime(s.get('entry_window_end_kst','08:00'),'%H:%M').time()
    return (snap,asof) if start<=asof.time()<=end else None

def archive_closed_slot(st,coin):
    old=st.get('positions',{}).get(coin)
    if old and old.get('status')=='CLOSED': st.setdefault('closed_positions',[]).append(dict(old,coin=coin))

def buy_position(st,row,px,budget,session,sid,date):
    if px is None or budget<=0 or float(st.get('cash_krw',0))+1e-9<budget: return False
    slip=float(CFG.get('slippage_rate',.001)); fee=float(CFG.get('fee_rate',.0005)); fill=float(px)*(1+slip); notional=budget/(1+fee); coin=row['coin']; archive_closed_slot(st,coin)
    st.setdefault('positions',{})[coin]={'market':row['market'],'status':'OPEN','entry_at':now_iso(),'entry_session':session,'entry_session_id':sid,'first_selected_date':date,'last_selected_date':date,'consecutive_top10_days':1,'signal_rank':row.get('Rank'),'signal_vpd':row.get('VPD'),'signal_price':row.get('price'),'entry_market_price':float(px),'entry_price':fill,'buy_notional_krw':notional,'buy_fee_krw':budget-notional,'qty':notional/fill,'cost_krw':budget,'target_profit_pct':float(CFG['exit']['take_profit_pct']),'stop_loss_pct':float(CFG['exit']['hard_stop_pct']),'warning_profit_pct':float(CFG['exit']['warning_profit_pct']),'last_price':float(px),'peak_price':float(px),'tp_warning_sent':False,'sl_warning_sent':False}
    st['cash_krw']=float(st.get('cash_krw',0))-budget
    log_event({'ts':now_iso(),'type':'BUY','cohort_id':st.get('cohort_id'),'session_id':sid,'entry_session':session,'coin':coin,'market':row['market'],'budget_krw':round(budget,2),'buy_notional_krw':round(notional,2),'buy_fee_krw':round(budget-notional,2),'market_price':float(px),'fill_price':fill,'target_profit_pct':float(CFG['exit']['take_profit_pct']),'stop_loss_pct':float(CFG['exit']['hard_stop_pct']),'signal_rank':row.get('Rank'),'signal_vpd':row.get('VPD'),'paper_only':True})
    return True

def close_position(st,coin,p,px,reason,send_status=True):
    if p.get('status','OPEN')!='OPEN': return False
    fee=float(CFG.get('fee_rate',.0005)); gross=float(p['qty'])*float(px); sell_fee=gross*fee; proceeds=gross-sell_fee; pnl=proceeds-float(p['cost_krw']); ret=(proceeds/float(p['cost_krw'])-1)*100
    p.update(status='CLOSED',exit_at=now_iso(),exit_price=float(px),sell_fee_krw=sell_fee,net_proceeds_krw=proceeds,pnl_krw=pnl,return_pct=ret,exit_reason=reason)
    st['cash_krw']=float(st.get('cash_krw',0))+proceeds; st['realized_pnl_krw']=float(st.get('realized_pnl_krw',0))+pnl; st['lifetime_realized_pnl_krw']=float(st.get('lifetime_realized_pnl_krw',0))+pnl
    log_event({'ts':now_iso(),'type':'SELL','cohort_id':st.get('cohort_id'),'coin':coin,'market':p['market'],'reason':reason,'market_price':float(px),'sell_fee_krw':round(sell_fee,2),'net_proceeds_krw':round(proceeds,2),'pnl_krw':round(pnl,2),'return_pct':round(ret,4),'entry_at':p.get('entry_at'),'entry_session':p.get('entry_session','AM'),'paper_only':True}); save_state(st)
    telegram(f'🔴 MAGI2 PAPER 청산\n{coin} / {reason}\n매수원금 {float(p["cost_krw"]):,.0f}원\n순수익률 {ret:+.2f}% / 손익 {pnl:+,.0f}원\n목표 +{float(p.get("target_profit_pct",12)):.1f}% / 손절 {float(p.get("stop_loss_pct",-6)):.1f}%\nPAPER ONLY')
    if send_status: send_current_status(st,'📊 청산 후 MAGI2 PAPER 현황')
    return True

def monitor_once(st):
    active={c:p for c,p in st.get('positions',{}).items() if p.get('status','OPEN')=='OPEN'}; prices=get_prices([p['market'] for p in active.values()]); distance=float(CFG.get('exit',{}).get('warning_distance_pct_point',2.0)); dirty=False
    for coin,p in list(active.items()):
        px=prices.get(p['market'])
        if px is None: continue
        p['last_price']=float(px); p['peak_price']=max(float(p.get('peak_price',px)),float(px)); ret=position_return(p,px); tp=float(p.get('target_profit_pct',12)); sl=float(p.get('stop_loss_pct',-6))
        if ret>=tp: close_position(st,coin,p,px,'TAKE_PROFIT'); continue
        if ret<=sl: close_position(st,coin,p,px,'HARD_STOP'); continue
        if ret>=tp-distance and not p.get('tp_warning_sent',False): telegram(f'⚠️ MAGI2 TP 접근\n{coin} | 원금 {float(p["cost_krw"]):,.0f}원 | 현재 {ret:+.2f}% | TP +{tp:.1f}%\nPAPER ONLY'); p['tp_warning_sent']=True; dirty=True
        elif ret<tp-distance-1 and p.get('tp_warning_sent',False): p['tp_warning_sent']=False; dirty=True
        if ret<=sl+distance and not p.get('sl_warning_sent',False): telegram(f'⚠️ MAGI2 SL 접근\n{coin} | 원금 {float(p["cost_krw"]):,.0f}원 | 현재 {ret:+.2f}% | SL {sl:.1f}%\nPAPER ONLY'); p['sl_warning_sent']=True; dirty=True
        elif ret>sl+distance+1 and p.get('sl_warning_sent',False): p['sl_warning_sent']=False; dirty=True
    if dirty: save_state(st)

def derive_daily_equal_buy(st,today):
    if st.get('daily_equal_buy_date')==today and float(st.get('daily_equal_buy_krw',0) or 0)>0:
        return float(st['daily_equal_buy_krw'])
    # Transition/fallback: if today's reference is missing or zero, derive it from
    # the actual OPEN-position cost average. This preserves the portfolio's real
    # equal-buy size instead of hardcoding 300,000. With the current cohort this
    # naturally resolves to 300,000 KRW.
    costs=[float(p.get('cost_krw',0)) for p in st.get('positions',{}).values() if p.get('status')=='OPEN' and float(p.get('cost_krw',0))>0]
    if not costs: return None
    slot=sum(costs)/len(costs)
    st['daily_equal_buy_krw']=slot; st['daily_equal_buy_date']=today; st['daily_equal_buy_source']='OPEN_POSITION_AVG_FALLBACK'; save_state(st)
    return slot

def morning_rebalance(st):
    loaded=load_morning_snapshot()
    if not loaded: raise RuntimeError('Fresh morning VPD snapshot (today 07:20~08:00 KST) is unavailable.')
    snap,asof=loaded; today=asof.date().isoformat()
    if st.get('last_rebalance_date')==today: telegram('ℹ️ MAGI2 MORNING\n오늘 AM 리밸런싱은 이미 완료되었습니다.\nPAPER ONLY'); return False
    # Reset the daily PnL bucket before today's exits so VPD_EXIT PnL is retained.
    st['realized_pnl_krw']=0.0
    top_n=int(CFG.get('session',{}).get('top_n',10)); candidates=snap.get('top10',[])[:top_n]
    if not candidates: raise RuntimeError('Morning VPD TOP10 is empty.')
    top={r['coin']:r for r in candidates}; active={c:p for c,p in st.get('positions',{}).items() if p.get('status')=='OPEN'}; prices=get_prices(list({p['market'] for p in active.values()}|{r['market'] for r in candidates})); kept=[]; exited=[]; bought=[]; sid=f'{today}-AM-001'
    for coin,p in list(active.items()):
        if coin in top:
            p['last_selected_date']=today; p['consecutive_top10_days']=int(p.get('consecutive_top10_days',1))+1; p['signal_rank']=top[coin].get('Rank'); p['signal_vpd']=top[coin].get('VPD'); kept.append(coin)
            log_event({'ts':now_iso(),'type':'KEEP','cohort_id':sid,'coin':coin,'market':p['market'],'signal_rank':p.get('signal_rank'),'signal_vpd':p.get('signal_vpd'),'consecutive_top10_days':p['consecutive_top10_days'],'entry_at':p.get('entry_at'),'entry_session':p.get('entry_session','AM'),'paper_only':True})
        else:
            px=prices.get(p['market'])
            if px is not None and close_position(st,coin,p,px,'VPD_EXIT',False): exited.append(coin)
    open_after={c:p for c,p in st.get('positions',{}).items() if p.get('status')=='OPEN'}; morning_slot=float(CFG.get('position_krw',300000))
    for row in candidates:
        if len(open_after)>=top_n: break
        if row['coin'] in open_after: continue
        if float(st.get('cash_krw',0))+1e-9<morning_slot: break
        if buy_position(st,row,prices.get(row['market']),morning_slot,'AM',sid,today): bought.append(row['coin']); open_after[row['coin']]=st['positions'][row['coin']]
    st['cohort_id']=sid; st['cohort_date']=today; st['last_rebalance_date']=today; st['source_snapshot_asof_kst']=snap.get('asof_kst',asof.isoformat()); st['strategy']=CFG.get('paper_strategy','VPD_TOP10_EQUAL_WEIGHT'); st['cohort_policy']='AM_ROLLING_TOP10_COMMAND_REFILL'; st['capital_model']='DAILY_EQUAL_BUY_REFILL'
    current=[p for p in st.get('positions',{}).values() if p.get('status')=='OPEN']; slot=sum(float(p['cost_krw']) for p in current)/len(current) if current else 0; st['daily_equal_buy_krw']=slot; st['daily_equal_buy_date']=today; st['daily_equal_buy_source']='MORNING_OPEN_AVG'; save_state(st)
    telegram(f"🔄 MAGI2 MORNING 리밸런싱 완료\n{sid}\nKEEP {len(kept)}: {', '.join(kept) or '-'}\nSELL {len(exited)}: {', '.join(exited) or '-'}\nBUY {len(bought)}: {', '.join(bought) or '-'}\n당일 균등매수원가 {slot:,.0f}원\n※ 중복 검출 종목 유지 · PAPER ONLY"); send_current_status(st,'📊 AM 리밸런싱 후 MAGI2 PAPER 현황'); return True

def refill(st):
    loaded=load_today_snapshot()
    if not loaded: raise RuntimeError("Today's VPD snapshot is unavailable. Refill aborted.")
    snap,asof=loaded; today=now_dt().date().isoformat(); slot=derive_daily_equal_buy(st,today)
    if not slot: raise RuntimeError("Today's daily_equal_buy_krw is unavailable. No OPEN-position average exists.")
    top_n=int(CFG.get('session',{}).get('top_n',10)); active={c:p for c,p in st.get('positions',{}).items() if p.get('status')=='OPEN'}; vacant=max(0,top_n-len(active)); cash=float(st.get('cash_krw',0)); count=min(vacant,int((cash+1e-9)//slot))
    if count<=0: telegram(f'ℹ️ MAGI2 REFILL\n빈자리 {vacant} / 예수금 {cash:,.0f}원 / 당일 균등매수원가 {slot:,.0f}원\n리필 가능한 슬롯이 없습니다.\nPAPER ONLY'); send_current_status(st); return False
    rows=[r for r in snap.get('top10',[])[:top_n] if r['coin'] not in active][:count]
    if not rows: telegram('ℹ️ MAGI2 REFILL\n현재 VPD TOP10에 신규 리필 후보가 없습니다.\nPAPER ONLY'); send_current_status(st); return False
    prices=get_prices([r['market'] for r in rows]); sid=f'{today}-REFILL-{now_dt().strftime("%H%M")}'; bought=[]
    for row in rows:
        if buy_position(st,row,prices.get(row['market']),slot,'REFILL',sid,today): bought.append(row['coin'])
    if not bought: raise RuntimeError('Refill candidates existed, but no PAPER buy could be executed.')
    st['last_refill_at']=now_iso(); st['last_refill_session_id']=sid; st['source_refill_snapshot_asof_kst']=snap.get('asof_kst',asof.isoformat()); save_state(st)
    details='\n'.join(f'{i+1}. {c} | 매수금액 {slot:,.0f}원' for i,c in enumerate(bought)); telegram(f'♻️ MAGI2 REFILL 완료\n{sid}\nBUY {len(bought)}\n{details}\n당일 균등매수원가 {slot:,.0f}원\n잔여 예수금 {float(st["cash_krw"]):,.0f}원\n※ 기존 보유 종목 매도 없음 · PAPER ONLY'); send_current_status(st,'📊 REFILL 후 MAGI2 PAPER 현황'); return True

def report(st):
    active=[p['market'] for p in st.get('positions',{}).values() if p.get('status','OPEN')=='OPEN']; telegram(portfolio_status(st,get_prices(active)))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('mode',nargs='?',default='monitor',choices=['monitor','morning','refill','report']); mode=ap.parse_args().mode; st=load_state(); migrate_state(st)
    if mode=='monitor': monitor_once(st)
    elif mode=='morning': morning_rebalance(st)
    elif mode=='refill': refill(st)
    else: report(st)

if __name__=='__main__': main()
