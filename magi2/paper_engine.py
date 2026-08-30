"""MAGI 2 Paper Execution Engine POC.

Consumes MAGI 1 vpd_latest.json and simulates entries/exits only.
NO real exchange orders. State is persisted in data/magi2_paper_state.json.
"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/'magi2/config.json').read_text(encoding='utf-8'))
LATEST=json.loads((ROOT/'data/vpd_latest.json').read_text(encoding='utf-8'))
STATE_PATH=ROOT/'data/magi2_paper_state.json'
LOG_PATH=ROOT/'data/magi2_trade_log.jsonl'
KST=ZoneInfo('Asia/Seoul')

def now(): return datetime.now(KST).isoformat()

def load_state():
    if STATE_PATH.exists(): return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    return {'mode':'PAPER','cash_krw':CFG['initial_cash_krw'],'positions':{},'realized_pnl_krw':0.0,'updated_at':now()}

def log(event):
    LOG_PATH.parent.mkdir(parents=True,exist_ok=True)
    with LOG_PATH.open('a',encoding='utf-8') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')

def rows():
    # tolerate common latest-json layouts
    for key in ('top10','rows','results','data'):
        v=LATEST.get(key) if isinstance(LATEST,dict) else None
        if isinstance(v,list): return v
    return LATEST if isinstance(LATEST,list) else []

def field(r,*names,default=None):
    for n in names:
        if n in r:return r[n]
    return default

def main():
    st=load_state(); cfg=CFG; universe=rows(); by_coin={field(r,'coin','Coin','symbol'):r for r in universe}
    events=[]
    # exits first
    for coin,p in list(st['positions'].items()):
        r=by_coin.get(coin)
        if not r: continue
        px=float(field(r,'price','Price',default=p['last_price']))
        p['last_price']=px; p['peak_price']=max(float(p.get('peak_price',p['entry_price'])),px)
        ret=(px/p['entry_price']-1)*100
        peak_ret=(p['peak_price']/p['entry_price']-1)*100
        reason=None
        if ret>=cfg['exit']['take_profit_pct']: reason='TAKE_PROFIT'
        elif ret<=cfg['exit']['hard_stop_pct']: reason='HARD_STOP'
        elif peak_ret>=cfg['exit']['trailing_activation_pct'] and (px/p['peak_price']-1)*100<=-cfg['exit']['trailing_drawdown_pct']: reason='TRAILING_STOP'
        elif ret>=cfg['exit']['warning_profit_pct'] and not p.get('warning_sent'):
            p['warning_sent']=True; events.append({'ts':now(),'type':'PROFIT_WARNING','coin':coin,'return_pct':round(ret,2),'price':px})
        if reason:
            gross=p['qty']*px; fee=gross*cfg['fee_rate']; proceeds=gross-fee; pnl=proceeds-p['cost_krw']
            st['cash_krw']+=proceeds; st['realized_pnl_krw']+=pnl
            e={'ts':now(),'type':'SELL','coin':coin,'reason':reason,'price':px,'qty':p['qty'],'pnl_krw':round(pnl,2),'return_pct':round(ret,2)}; events.append(e); log(e); del st['positions'][coin]
    # entries from current MAGI 1 candidates
    for r in universe:
        if len(st['positions'])>=cfg['max_open_positions']: break
        coin=field(r,'coin','Coin','symbol'); trig=str(field(r,'trigger','Trigger',default='-')); mom=str(field(r,'momentum','Momentum',default=''))
        vpd=float(field(r,'VPD','vpd',default=0) or 0); px=field(r,'price','Price')
        if not coin or px is None or coin in st['positions']: continue
        allowed=(trig=='A' and cfg['entry']['trigger_a']) or (trig=='B' and cfg['entry']['trigger_b'])
        if not allowed or vpd<cfg['entry']['min_vpd']: continue
        if cfg['entry']['require_momentum'] and mom not in ('↑','↑↑'): continue
        budget=min(cfg['max_position_krw'],st['cash_krw']);
        if budget<=0: break
        fill=float(px)*(1+cfg['slippage_rate']); fee=budget*cfg['fee_rate']; invest=budget-fee; qty=invest/fill
        st['cash_krw']-=budget
        p={'entry_at':now(),'entry_price':fill,'last_price':float(px),'peak_price':float(px),'qty':qty,'cost_krw':budget,'entry_vpd':vpd,'entry_momentum':mom,'entry_trigger':trig,'warning_sent':False}
        st['positions'][coin]=p
        e={'ts':now(),'type':'BUY','coin':coin,'trigger':trig,'vpd':vpd,'momentum':mom,'market_price':float(px),'fill_price':fill,'budget_krw':budget}; events.append(e); log(e)
    st['updated_at']=now(); STATE_PATH.write_text(json.dumps(st,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'mode':'PAPER','events':events,'state':st},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
