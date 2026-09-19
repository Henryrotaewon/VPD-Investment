import argparse, csv, io, json, os, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from magi2.hold_policy import POLICY, assess_hold, decision_text, protection
from magi2.point_in_time_scan import read_snapshot, build_snapshot, cutoff_for, SCHEMA, MAX_AGE_SECONDS

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

def price_return(p,px):
    entry=float(p.get('entry_market_price',p.get('entry_price',0)) or 0)
    return (float(px)/entry-1)*100 if entry else 0.0

def portfolio_status(st,prices,title='📊 VPD 모의투자 현황 [PAPER]'):
    fee=float(CFG.get('fee_rate',.0005)); cash=float(st.get('cash_krw',0)); value=0; rows=[]
    for coin,p in st.get('positions',{}).items():
        if p.get('status','OPEN')!='OPEN': continue
        px=prices.get(p['market'],float(p.get('last_price',p['entry_price']))); value+=float(p['qty'])*px*(1-fee)
        entry=float(p.get('entry_market_price',p.get('entry_price',0)) or 0)
        rows.append((coin,price_return(p,px),position_return(p,px),p.get('entry_session','AM'),float(p['cost_krw']),float(p.get('target_profit_pct',12)),float(p.get('stop_loss_pct',-6)),entry,float(px)))
    base=float(st.get('initial_cash_krw',CFG['initial_cash_krw'])); equity=cash+value; ret=(equity/base-1)*100 if base else 0; invested=sum(x[4] for x in rows)
    lines=[title,f"Cohort {st.get('cohort_id','-')}",f'최초원금 {base:,.0f}원',f'매수원금 합계 {invested:,.0f}원 / 예수금 {cash:,.0f}원',f'평가 {equity:,.0f}원 / 누적 {ret:+.2f}% ({equity-base:+,.0f}원)']
    slot=float(st.get('daily_equal_buy_krw',0) or 0)
    if slot>0: lines.append(f'당일 균등매수원가 {slot:,.0f}원')
    for c,r,net,s,cost,tp,sl,entry,px in sorted(rows,key=lambda x:x[1],reverse=True):
        tag=' REFILL' if s=='REFILL' else (' PM' if s=='PM_REFILL' else '')
        lines.append(f'{c}{tag} | 원금 {cost:,.0f}원 | 매수가 {entry:g} | 현재가 {px:g} | {r:+.2f}% | 순손익 {net:+.2f}% | TP +{tp:.1f}% / SL {sl:.1f}%')
    lines += [f"금일 실현손익 {float(st.get('realized_pnl_krw',0)):+,.0f}원",'수익률=매수가 대비 현재가 · 순손익=슬리피지+매수/매도 수수료 반영 · PAPER ONLY']
    return '\n'.join(lines)

def send_current_status(st,title='📊 VPD 모의투자 현황 [PAPER]'):
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

def parse_snapshot_asof(raw):
    if not raw: return None
    text=str(raw).strip()
    if text.endswith(' KST'): text=text[:-4]+'+09:00'
    try:
        dt=datetime.fromisoformat(text)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except ValueError:
        return None

def load_latest_snapshot():
    repo=os.getenv('MAGI_GITHUB_REPO','Henryrotaewon/VPD-Investment').strip()
    url=f'https://raw.githubusercontent.com/{repo}/main/data/vpd_latest.json'
    try:
        r=requests.get(url,timeout=20); r.raise_for_status(); snap=r.json()
    except Exception as e:
        print(f'VPD GitHub fetch error: {e}'); return None
    raw=snap.get('asof') or snap.get('asof_kst')
    asof=parse_snapshot_asof(raw)
    return (snap,asof) if asof else None

def load_today_snapshot():
    loaded=load_latest_snapshot()
    if not loaded: return None
    snap,asof=loaded
    return (snap,asof) if asof.date()==now_dt().date() else None

def load_morning_snapshot(expected_asof=None):
    snap=read_snapshot(STATE_PATH.resolve().with_name('vpd_rebalance.json'),now_dt(),expected_asof)
    return (snap,parse_snapshot_asof(snap['asof'])) if snap else None

def load_all_ranked_rows(revision=None):
    repo=os.getenv('MAGI_GITHUB_REPO','Henryrotaewon/VPD-Investment').strip()
    ref=revision or 'main'
    url=f'https://raw.githubusercontent.com/{repo}/{ref}/data/vpd_all_latest.csv?t={int(now_dt().timestamp())}'
    try:
        r=requests.get(url,timeout=20); r.raise_for_status(); rows={}
        for raw in csv.DictReader(io.StringIO(r.text.lstrip('\ufeff'))):
            coin=(raw.get('coin') or raw.get('Coin') or '').strip()
            if coin: rows[coin]=raw
        return rows
    except Exception as e:
        print(f'VPD all-rank fetch error: {e}'); return {}

def _number(row,*names):
    for name in names:
        value=row.get(name)
        if value not in (None,''):
            try: return float(value)
            except (TypeError,ValueError): pass
    return None

def hold_signal(row):
    """Return whether an existing position still has at least N live VPD signals."""
    cfg=CFG.get('hold',{}); checks={
        'VPD': (_number(row,'VPD') or 0)>=float(cfg.get('min_vpd',60)),
        'MOMENTUM': str(row.get('momentum','')).strip() in {'↑','↑↑'},
        'VELOCITY': (_number(row,'VPDVelocity') or 0)>0,
        'INTRA_ACCEL': (_number(row,'IntraAccel') or 0)>float(cfg.get('min_intra_accel',1)),
        'TODAY_VALUE': (_number(row,'TodayValue/10') or 0)>float(cfg.get('min_today_value_ratio',1)),
        'GIVEBACK_SAFE': (_number(row,'Giveback%p') is not None and _number(row,'Giveback%p')<float(cfg.get('max_giveback_pct_point',10))),
    }
    passed=[name for name,ok in checks.items() if ok]
    return len(passed)>=int(cfg.get('min_alive_signals',2)),passed,checks

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
    if send_status: send_current_status(st,'📊 청산 후 VPD 모의투자 현황 [PAPER]')
    return True

def monitor_once(st):
    active={c:p for c,p in st.get('positions',{}).items() if p.get('status','OPEN')=='OPEN'}; prices=get_prices([p['market'] for p in active.values()]); distance=float(CFG.get('exit',{}).get('warning_distance_pct_point',2.0)); dirty=False
    for coin,p in list(active.items()):
        px=prices.get(p['market'])
        if px is None: continue
        risk=protection(p,px,CFG)
        if risk['kind']=='DATA_WAIT': continue
        peak=risk['observed_peak_price']
        if p.get('peak_price')!=peak: dirty=True
        p['last_price']=float(px); p['peak_price']=peak; ret=risk['net_return_pct']; tp=float(p.get('target_profit_pct',12)); sl=float(p.get('stop_loss_pct',-6))
        if risk['kind']:
            close_position(st,coin,p,px,risk['kind']); continue
        if ret>=tp-distance and not p.get('tp_warning_sent',False): telegram(f'⚠️ MAGI2 TP 접근\n{coin} | 원금 {float(p["cost_krw"]):,.0f}원 | 현재 {ret:+.2f}% | TP +{tp:.1f}%\nPAPER ONLY'); p['tp_warning_sent']=True; dirty=True
        elif ret<tp-distance-1 and p.get('tp_warning_sent',False): p['tp_warning_sent']=False; dirty=True
        if ret<=sl+distance and not p.get('sl_warning_sent',False): telegram(f'⚠️ MAGI2 SL 접근\n{coin} | 원금 {float(p["cost_krw"]):,.0f}원 | 현재 {ret:+.2f}% | SL {sl:.1f}%\nPAPER ONLY'); p['sl_warning_sent']=True; dirty=True
        elif ret>sl+distance+1 and p.get('sl_warning_sent',False): p['sl_warning_sent']=False; dirty=True
    if dirty: save_state(st)

def derive_daily_equal_buy(st,today):
    if st.get('daily_equal_buy_date')==today and float(st.get('daily_equal_buy_krw',0) or 0)>0:
        return float(st['daily_equal_buy_krw'])
    costs=[float(p.get('cost_krw',0)) for p in st.get('positions',{}).values() if p.get('status')=='OPEN' and float(p.get('cost_krw',0))>0]
    if not costs: return None
    slot=sum(costs)/len(costs)
    st['daily_equal_buy_krw']=slot; st['daily_equal_buy_date']=today; st['daily_equal_buy_source']='OPEN_POSITION_AVG_FALLBACK'; save_state(st)
    return slot

def snapshot_issue(asof, now, snapshot=None):
    snap=snapshot or {}
    generated=parse_snapshot_asof(snap.get('generated_at'))
    if (snap.get('schema')!=SCHEMA or snap.get('basis')!='POINT_IN_TIME' or
            not generated or asof!=cutoff_for(asof) or not asof<=generated<=now):
        return '요청 시점에 맞춘 VPD 스캔을 기다리고 있습니다.'
    if not 0<=(now-asof).total_seconds()<=MAX_AGE_SECONDS:
        return 'VPD 자료가 오래됐거나 시각 확인이 필요합니다.'
    return None


def morning_rebalance(st,expected_asof=None):
    loaded=load_morning_snapshot(expected_asof)
    if not loaded:
        telegram('⏳ VPD 리밸런싱 자료 대기\n요청 시점에 맞춘 새 스캔이 아직 준비되지 않았습니다. 리밸런싱 버튼으로 다시 요청하세요.\n보유 판단·매매 없음'); return False
    snap,asof=loaded; today=now_dt().date().isoformat(); snapshot_raw=snap.get('asof') or snap.get('asof_kst') or asof.isoformat()
    issue=snapshot_issue(asof,now_dt(),snap)
    if issue:
        telegram(f'⏳ VPD 리밸런싱 대기\n{issue}\n마지막 스캔: {snap.get("asof_kst",snapshot_raw)}\n보유 판단·매매 없음'); return False
    last_raw=st.get('last_rebalance_vpd_asof') or st.get('source_snapshot_asof_kst')
    last_asof=parse_snapshot_asof(last_raw)
    if last_asof and asof<=last_asof:
        telegram(f'ℹ️ VPD 리밸런싱 · 이미 평가한 자료\n자료 기준: {snap.get("asof_kst",snapshot_raw)}\n보유 판단과 매매를 반복하지 않았습니다.\n※ 이 메시지는 새 홀딩 판정이 아닙니다.'); return False
    top_n=int(CFG.get('session',{}).get('top_n',10)); candidates=snap.get('top10',[])[:top_n]
    if not candidates: raise RuntimeError('Latest VPD TOP10 is empty.')
    top={r['coin']:r for r in candidates}; all_rows=snap.get('all_rows') or {}; active={c:p for c,p in st.get('positions',{}).items() if p.get('status')=='OPEN'}; prices=get_prices(list({p['market'] for p in active.values()}|{r['market'] for r in candidates})); kept=[]; weakening=[]; exited=[]; bought=[]; waiting=[]; decisions=[]; stage='REQUEST'; sid=f'{asof:%Y-%m-%d}-VPD-{asof:%H%M}'
    assessments={coin:assess_hold(all_rows.get(coin) or top.get(coin,{}),p,prices.get(p['market']),CFG) for coin,p in active.items()}
    incomplete=[coin for coin,result in assessments.items() if result['kind']=='DATA_WAIT']
    if incomplete:
        # Do not consume this snapshot: the same request can be retried once
        # data recovers, without repeating another position's grace count.
        telegram(f'⏳ VPD 리밸런싱 자료 대기 {len(incomplete)}\n'+', '.join(incomplete)+'\n가격·보유 추세 자료를 확인한 뒤 다시 실행하세요.\n보유 판단·매매·약화 횟수 변경 없음'); return False
    if st.get('last_rebalance_date')!=today: st['realized_pnl_krw']=0.0
    for coin,p in list(active.items()):
        row=all_rows.get(coin) or top.get(coin,{})
        px=prices.get(p['market']); assessment=assessments[coin]; kind=assessment['kind']; suffix=''
        p['hold_assessment']=dict(assessment,asof=asof.isoformat())
        if 'net_return_pct' in assessment:
            p['last_price']=float(px); p['peak_price']=assessment['observed_peak_price']
        p['signal_rank']=_number(row,'Rank'); p['signal_vpd']=_number(row,'VPD')
        p['hold_signal_reasons']=assessment['reasons']; p['hold_signal_checked_at']=now_iso()
        if kind=='DATA_WAIT':
            waiting.append(coin); suffix='기존 보유 유지 · 판단 유예'
        elif kind in {'HARD_STOP','TAKE_PROFIT','PROFIT_PROTECTION'}:
            if close_position(st,coin,p,px,kind,False): exited.append(coin)
        elif kind in {'TREND','PROFIT'}:
            p['hold_state']=kind; p['weakening_count']=0
            p.pop('weakening_since',None); p['weakening_policy']=POLICY
            if coin in top:
                if p.get('last_selected_date')!=today: p['consecutive_top10_days']=int(p.get('consecutive_top10_days',0))+1
                p['last_selected_date']=today
            kept.append(coin)
        else:
            # Old policy counts cannot cause immediate liquidation on adoption.
            if p.get('weakening_policy')!=POLICY:
                p['weakening_count']=0; p.pop('weakening_since',None)
            since=parse_snapshot_asof(p.get('weakening_since')) or asof
            elapsed=max(0.,(asof-since).total_seconds()/3600)
            count=int(p.get('weakening_count',0))+1
            p.update(hold_state='WEAKENING',weakening_count=count,weakening_since=since.isoformat(),weakening_policy=POLICY)
            hold=CFG.get('hold',{}); grace=float(hold.get('weakening_grace_hours',24))
            if count>=int(hold.get('weakening_confirmations',2)) and elapsed>=grace:
                if close_position(st,coin,p,px,'VPD_SIGNAL_DECAY',False): exited.append(coin)
                suffix=f'약화 {count}회 · {elapsed:.1f}시간 지속 → 청산'
            else:
                weakening.append(coin); suffix=f'약화 {count}회 · {elapsed:.1f}/{grace:g}시간 · 매도 유예'
        decisions.append(decision_text(coin,row,assessment,suffix))
        log_event({'ts':now_iso(),'type':'HOLD_ASSESSMENT','cohort_id':sid,'coin':coin,'market':p['market'],'snapshot_asof':asof.isoformat(),'assessment':assessment,'weakening_count':p.get('weakening_count',0),'decision_note':suffix,'paper_only':True})
    open_after={c:p for c,p in st.get('positions',{}).items() if p.get('status')=='OPEN'}
    remaining_slots=max(0,top_n-len(open_after))
    available_cash=float(st.get('cash_krw',0))
    if remaining_slots>0:
        morning_slot=available_cash/remaining_slots
        daily_source='MORNING_AVAILABLE_CASH_PER_NEW_SLOT'
    else:
        costs=[float(p.get('cost_krw',0)) for p in open_after.values() if float(p.get('cost_krw',0))>0]
        morning_slot=sum(costs)/len(costs) if costs else 0.0
        daily_source='MORNING_ALL_KEEP_OPEN_AVG'
    for row in candidates:
        if len(open_after)>=top_n: break
        if row['coin'] in open_after or row['coin'] in exited: continue
        old=st.get('positions',{}).get(row['coin'],{})
        old_exit=parse_snapshot_asof(old.get('exit_at'))
        if old.get('status')=='CLOSED' and old_exit and old_exit.date().isoformat()==today:
            decisions.append(f'{row["coin"]} · 재진입 유예\n  오늘 청산한 종목은 당일 리밸런싱에서 다시 매수하지 않습니다.')
            continue
        if buy_position(st,row,prices.get(row['market']),morning_slot,stage,sid,today): bought.append(row['coin']); open_after[row['coin']]=st['positions'][row['coin']]
    st['cohort_id']=sid; st['cohort_date']=today; st['last_rebalance_date']=today; st['last_rebalance_vpd_asof']=asof.isoformat(); st['source_snapshot_asof_kst']=snap.get('asof_kst',asof.isoformat()); st['strategy']=CFG.get('paper_strategy','VPD_TOP10_EQUAL_WEIGHT'); st['cohort_policy']='ANYTIME_TREND_PROFIT_TIME_GRACE_COMMAND_REFILL'; st['capital_model']='ROLLING_KEEP_DYNAMIC_NEW_SLOT'
    st['source_snapshot_revision']=snap.get('_source_revision')
    st['daily_equal_buy_krw']=morning_slot; st['daily_equal_buy_date']=today; st['daily_equal_buy_source']=daily_source
    result={'asof':asof.isoformat(),'kept':kept,'weakening':weakening,'sold':exited,'bought':bought,
            'cash_krw':round(st['cash_krw'],2),'paper_only':True}
    st['last_rebalance_result']=result
    st['last_rebalance_decisions']=decisions; st['hold_policy']=POLICY; save_state(st)
    log_event(dict(result,ts=now_iso(),type='REBALANCE_FINISHED',session_id=sid))
    print('VPD_REBALANCE_RESULT '+json.dumps(result,ensure_ascii=False),flush=True)
    for decision in decisions: print('VPD_DECISION '+decision.replace('\n',' | '),flush=True)
    phase='요청 시점 재점검'
    summary=(f"🔄 VPD 모의투자 · {phase}\n기준 {snap.get('asof_kst',asof.isoformat())}\n"
             f"추세·수익 보호 보유 {len(kept)} / 약화 유예 {len(weakening)} / 자료 대기 {len(waiting)}\n"
             f"매도 {len(exited)}: {', '.join(exited) or '-'} / 매수 {len(bought)}: {', '.join(bought) or '-'}")
    if not remaining_slots: summary+='\n신규 편입 없음: 보유 슬롯이 모두 차 있습니다.'
    telegram(summary+'\n\n'+'\n'.join(decisions)+'\n\n모의투자 · 순수익은 모형 비용 반영')
    send_current_status(st,'📊 리밸런싱 후 VPD 모의투자 현황'); return True

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
    details='\n'.join(f'{i+1}. {c} | 매수금액 {slot:,.0f}원' for i,c in enumerate(bought)); telegram(f'♻️ MAGI2 REFILL 완료\n{sid}\nBUY {len(bought)}\n{details}\n당일 균등매수원가 {slot:,.0f}원\n잔여 예수금 {float(st["cash_krw"]):,.0f}원\n※ 기존 보유 종목 매도 없음 · PAPER ONLY'); send_current_status(st,'📊 REFILL 후 VPD 모의투자 현황 [PAPER]'); return True

def report(st):
    active=[p['market'] for p in st.get('positions',{}).values() if p.get('status','OPEN')=='OPEN']; telegram(portfolio_status(st,get_prices(active)))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('mode',nargs='?',default='monitor',choices=['monitor','morning','refill','report'])
    ap.add_argument('--snapshot-asof'); args=ap.parse_args(); mode=args.mode
    if mode=='morning' and not args.snapshot_asof:
        snapshot=build_snapshot(STATE_PATH.resolve().with_name('vpd_rebalance.json'),now_dt())
        args.snapshot_asof=snapshot['asof']
    st=load_state(); migrate_state(st)
    if mode=='monitor': monitor_once(st)
    elif mode=='morning': morning_rebalance(st,args.snapshot_asof)
    elif mode=='refill': refill(st)
    else: report(st)

if __name__=='__main__': main()
