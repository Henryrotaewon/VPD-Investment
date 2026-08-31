import json, os, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT=Path(__file__).resolve().parents[1]; CFG=json.loads((ROOT/'magi2'/'config.json').read_text(encoding='utf-8')); STATE_PATH=ROOT/'magi2'/'state'/'paper_state.json'; EVENTS_PATH=ROOT/'magi2'/'state'/'paper_events.jsonl'; VPD_PATH=ROOT/'data'/'vpd_latest.json'; KST=ZoneInfo('Asia/Seoul')
def now_dt(): return datetime.now(KST)
def now_iso(): return now_dt().isoformat()
def parse_hhmm(s): return datetime.strptime(s,'%H:%M').time()
def telegram(text):
 token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
 if not token or not chat:return
 try: requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':text},timeout=15).raise_for_status()
 except Exception as e: print(f'Telegram error: {e}')
def save_state(st): STATE_PATH.parent.mkdir(parents=True,exist_ok=True); st['updated_at']=now_iso(); STATE_PATH.write_text(json.dumps(st,ensure_ascii=False,indent=2),encoding='utf-8')
def load_state():
 if not STATE_PATH.exists(): raise RuntimeError('MAGI2 paper_state.json is missing. Refusing automatic reset.')
 return json.loads(STATE_PATH.read_text(encoding='utf-8'))
def log_event(e):
 EVENTS_PATH.parent.mkdir(parents=True,exist_ok=True)
 with EVENTS_PATH.open('a',encoding='utf-8') as f:f.write(json.dumps(e,ensure_ascii=False)+'\n')
def get_prices(markets):
 out={}
 for i in range(0,len(markets),100):
  try:
   r=requests.get('https://api.upbit.com/v1/ticker',params={'markets':','.join(markets[i:i+100])},timeout=15);r.raise_for_status()
   for x in r.json():out[x['market']]=float(x['trade_price'])
  except Exception as e:print('price error',e)
 return out
def position_return(p,px):
 fee=float(CFG.get('fee_rate',.0005));return(float(p['qty'])*px*(1-fee)/float(p['cost_krw'])-1)*100
def portfolio_status(st,prices,title='📊 MAGI2 PAPER 현황'):
 cash=float(st.get('cash_krw',0));rows=[];open_value=0;fee=float(CFG.get('fee_rate',.0005))
 for coin,p in st.get('positions',{}).items():
  if p.get('status','OPEN')!='OPEN':continue
  px=prices.get(p['market'],float(p.get('last_price',p['entry_price'])));open_value+=float(p['qty'])*px*(1-fee);rows.append((coin,position_return(p,px),p.get('entry_session','AM'),float(p['cost_krw']),float(p.get('target_profit_pct',12)),float(p.get('stop_loss_pct',-6))))
 base=float(st.get('initial_cash_krw',CFG['initial_cash_krw']));equity=cash+open_value;ret=(equity/base-1)*100;invested=sum(x[3] for x in rows)
 lines=[title,f"Cohort {st.get('cohort_id','-')}",f'최초원금 {base:,.0f}원',f'매수원금 합계 {invested:,.0f}원 / 예수금 {cash:,.0f}원',f'평가 {equity:,.0f}원 / 누적 {ret:+.2f}% ({equity-base:+,.0f}원)']
 for c,r,s,cost,tp,sl in sorted(rows,key=lambda x:x[1],reverse=True):lines.append(f"{c}{' PM' if s=='PM_REFILL' else ''} | 원금 {cost:,.0f}원 | {r:+.2f}% | TP +{tp:.1f}% / SL {sl:.1f}%")
 lines += [f"금일 실현손익 {float(st.get('realized_pnl_krw',0)):+,.0f}원",'수익률=슬리피지+매수/매도 수수료 반영 · PAPER ONLY'];return '\n'.join(lines)
def send_current_status(st,title='📊 MAGI2 PAPER 현황'):
 active=[p['market'] for p in st.get('positions',{}).values() if p.get('status','OPEN')=='OPEN'];telegram(portfolio_status(st,get_prices(active),title))
def close_position(st,coin,p,px,reason):
 if p.get('status','OPEN')!='OPEN':return False
 fee=float(CFG.get('fee_rate',.0005));gross=float(p['qty'])*px;sell_fee=gross*fee;proceeds=gross-sell_fee;pnl=proceeds-float(p['cost_krw']);ret=(proceeds/float(p['cost_krw'])-1)*100;p.update(status='CLOSED',exit_at=now_iso(),exit_price=px,sell_fee_krw=sell_fee,net_proceeds_krw=proceeds,pnl_krw=pnl,return_pct=ret,exit_reason=reason);st['cash_krw']=float(st.get('cash_krw',0))+proceeds;st['realized_pnl_krw']=float(st.get('realized_pnl_krw',0))+pnl;st['lifetime_realized_pnl_krw']=float(st.get('lifetime_realized_pnl_krw',0))+pnl;log_event({'ts':now_iso(),'type':'SELL','coin':coin,'reason':reason,'pnl_krw':pnl,'return_pct':ret,'entry_session':p.get('entry_session','AM')});save_state(st);telegram(f'🔴 MAGI2 PAPER 청산\n{coin} / {reason}\n매수원금 {float(p["cost_krw"]):,.0f}원\n순수익률 {ret:+.2f}% / 손익 {pnl:+,.0f}원\n목표 +{float(p.get("target_profit_pct",12)):.1f}% / 손절 {float(p.get("stop_loss_pct",-6)):.1f}%\nPAPER ONLY');send_current_status(st,'📊 청산 후 MAGI2 PAPER 현황');return True
def migrate_buy_fee(st):
 fee=float(CFG.get('fee_rate',.0005));changed=False
 for p in st.get('positions',{}).values():
  if 'buy_fee_krw' not in p:
   cost=float(p.get('cost_krw',300000));n=cost/(1+fee);p['buy_notional_krw']=n;p['buy_fee_krw']=cost-n;p['qty']=n/float(p['entry_price']);changed=True
  if 'entry_session' not in p:p['entry_session']='AM';changed=True
 if changed:save_state(st)
def load_snapshot_window(a,b,ds,de):
 if not VPD_PATH.exists():return None
 snap=json.loads(VPD_PATH.read_text(encoding='utf-8'));raw=snap.get('asof')
 if not raw:return None
 asof=datetime.fromisoformat(raw).astimezone(KST);s=CFG.get('session',{});start=parse_hhmm(s.get(a,ds));end=parse_hhmm(s.get(b,de))
 return (snap,asof) if asof.date()==now_dt().date() and start<=asof.time()<=end else None
def archive_closed_slot(st,coin):
 old=st.get('positions',{}).get(coin)
 if old and old.get('status')=='CLOSED':st.setdefault('closed_positions',[]).append(dict(old,coin=coin))
def buy_position(st,row,px,budget,session,sid,date):
 if px is None or float(st.get('cash_krw',0))<budget:return False
 slip=float(CFG.get('slippage_rate',.001));fee=float(CFG.get('fee_rate',.0005));fill=px*(1+slip);notional=budget/(1+fee);coin=row['coin'];archive_closed_slot(st,coin);st['positions'][coin]={'market':row['market'],'status':'OPEN','entry_at':now_iso(),'entry_session':session,'entry_session_id':sid,'first_selected_date':date,'last_selected_date':date,'consecutive_top10_days':1,'signal_rank':row.get('Rank'),'signal_vpd':row.get('VPD'),'entry_market_price':px,'entry_price':fill,'buy_notional_krw':notional,'buy_fee_krw':budget-notional,'qty':notional/fill,'cost_krw':budget,'target_profit_pct':float(CFG['exit']['take_profit_pct']),'stop_loss_pct':float(CFG['exit']['hard_stop_pct']),'warning_profit_pct':float(CFG['exit']['warning_profit_pct']),'last_price':px,'peak_price':px};st['cash_krw']-=budget;log_event({'ts':now_iso(),'type':'BUY','coin':coin,'entry_session':session,'budget_krw':budget,'signal_rank':row.get('Rank'),'signal_vpd':row.get('VPD')});return True
def maybe_rebalance_morning(st):
 loaded=load_snapshot_window('entry_after_kst','entry_window_end_kst','07:20','08:00')
 if not loaded:return False
 snap,asof=loaded;today=asof.date().isoformat()
 if st.get('last_rebalance_date')==today:return False
 candidates=snap.get('top10',[])[:10];top={r['coin']:r for r in candidates};active={c:p for c,p in st['positions'].items() if p.get('status')=='OPEN'};prices=get_prices(list({p['market'] for p in active.values()}|{r['market'] for r in candidates}));kept=[];exited=[];bought=[]
 for c,p in active.items():
  if c in top:p['last_selected_date']=today;p['signal_rank']=top[c].get('Rank');p['signal_vpd']=top[c].get('VPD');kept.append(c)
  elif prices.get(p['market']) is not None:close_position(st,c,p,prices[p['market']],'VPD_EXIT');exited.append(c)
 slot=float(CFG.get('position_krw',300000));open_after={c for c,p in st['positions'].items() if p.get('status')=='OPEN'};sid=f'{today}-AM-001'
 for r in candidates:
  if r['coin'] not in open_after and st['cash_krw']>=slot and buy_position(st,r,prices.get(r['market']),slot,'AM',sid,today):bought.append(r['coin'])
 st['cohort_id']=sid;st['last_rebalance_date']=today;st['realized_pnl_krw']=0;save_state(st);telegram(f"🔄 MAGI2 AM TOP10 리밸런싱 완료\nKEEP {len(kept)}: {', '.join(kept) or '-'}\nSELL {len(exited)}: {', '.join(exited) or '-'}\nBUY {len(bought)}: {', '.join(bought) or '-'}");send_current_status(st,'📊 AM 리밸런싱 후 MAGI2 PAPER 현황');return True
def maybe_refill_evening(st):
 loaded=load_snapshot_window('evening_after_kst','evening_window_end_kst','17:50','18:20')
 if not loaded:return False
 snap,asof=loaded;today=asof.date().isoformat()
 if st.get('last_evening_refill_date')==today:return False
 slot=float(CFG.get('position_krw',300000));cash=float(st.get('cash_krw',0))
 if cash<slot:return False
 active={c for c,p in st['positions'].items() if p.get('status')=='OPEN'};rows=[r for r in snap.get('top10',[])[:10] if r['coin'] not in active][:int(cash//slot)];prices=get_prices([r['market'] for r in rows]);bought=[];sid=f'{today}-PM-REFILL'
 for r in rows:
  if buy_position(st,r,prices.get(r['market']),slot,'PM_REFILL',sid,today):bought.append(r['coin'])
 if not bought:return False
 st['last_evening_refill_date']=today;save_state(st);tp=float(CFG['exit']['take_profit_pct']);sl=float(CFG['exit']['hard_stop_pct']);details='\n'.join(f'{i+1}. {c} | 매수금액 {slot:,.0f}원 | TP +{tp:.1f}% / SL {sl:.1f}%' for i,c in enumerate(bought));telegram(f'🌙 MAGI2 EVENING REFILL 완료\n{sid}\nBUY {len(bought)}\n{details}\n잔여 예수금 {st["cash_krw"]:,.0f}원\nPAPER ONLY');send_current_status(st,'📊 PM 리필 후 MAGI2 PAPER 현황');return True
def monitor_once(st,send_status=False):
 active={c:p for c,p in st['positions'].items() if p.get('status')=='OPEN'};prices=get_prices([p['market'] for p in active.values()])
 if send_status:telegram(portfolio_status(st,prices))
 for c,p in list(active.items()):
  px=prices.get(p['market'])
  if px is None:continue
  p['last_price']=px;ret=position_return(p,px);tp=float(p.get('target_profit_pct',12));sl=float(p.get('stop_loss_pct',-6));warning=float(p.get('warning_profit_pct',10))
  if ret>=tp:close_position(st,c,p,px,'TAKE_PROFIT')
  elif ret<=sl:close_position(st,c,p,px,'HARD_STOP')
  elif ret>=warning:telegram(f'⚠️ MAGI2 PAPER 매도임박\n{c} / 매수금액 {p["cost_krw"]:,.0f}원 / 순수익률 {ret:+.2f}%\n목표 +{tp:.1f}%')
 save_state(st);return prices
def main():
 st=load_state();migrate_buy_fee(st);maybe_rebalance_morning(st);maybe_refill_evening(st);poll=int(CFG['monitor'].get('near_target_poll_seconds',60));deadline=time.time()+int(CFG['monitor'].get('run_window_minutes',14))*60;monitor_once(st,True)
 while time.time()+poll<=deadline:time.sleep(poll);monitor_once(st)
if __name__=='__main__':main()
