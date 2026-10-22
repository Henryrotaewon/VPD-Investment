"""Frozen entry-cap comparison; rebuild the entire path after every cancellation."""
import json
import math
from pathlib import Path
from research.five_asset_reentry_20260910.replay import (
    make_events,run as original_run,ms,clock,STEP,DAY,validate_data)
from research.ark_target_cycles.review import build_features
from research.september_exit_validation.replay import read_gzip

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'five_asset_reentry_20260910'


def run(rows,events,p,cap=None,delay=0):
    initial=p['capital_per_asset_krw'];cash=float(initial);realized=0.
    fee=p['fee_per_side'];slip=p['slippage_per_side']
    position=None;pending=None;cursor=0;cycle=0;last_exit=-1
    trades=[];cycles=[];path=[];decisions=[];canceled=[]
    for r in rows:
        t,op,hi,lo,close=r[:5]
        while cursor<len(events) and events[cursor]['available']<=t:
            e=events[cursor];cursor+=1
            reason=None
            if position:
                if e['kind']=='DAILY_EXIT':reason='DAILY_WEAKNESS_AND_PREVIOUS_LOW_BREAK'
                elif e['kind']=='HOUR' and e['available']>position['entry_ms'] and e['close']<position['initial_stop']:
                    reason='HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE'
            if reason:
                order=dict(side='SELL',available=e['available'],priority=e['priority'],reason=reason,
                           eligible=e['available']+delay*60000)
                if pending is None or pending['side']=='BUY' or (order['available'],order['priority'])<(pending['available'],pending['priority']):pending=order
            if e['kind']=='HOUR' and e['buy_candidate']:
                if position:status='IGNORED_WHILE_HOLDING'
                elif pending is not None or e['available']<=last_exit:status='IGNORED_PENDING_OR_STALE'
                else:
                    pending=dict(side='BUY',available=e['available'],priority=2,
                        initial_stop=e['initial_stop'],reference=e['close'],eligible=e['available']+delay*60000,
                        reason='FRESH_TWO_HOUR_DAILY_RECOVERY')
                    status='BUY_SCHEDULED'
                decisions.append(dict(signal_kst=clock(e['available']),day_kst=clock(e['day_ms'])[:5],status=status))
        if pending is not None and pending['eligible']<=t:
            order=pending;pending=None
            if order['side']=='BUY':
                assert position is None and cash>0
                fill=op*(1+slip);chase_pct=(fill/order['reference']-1)*100
                if cap is not None and chase_pct>cap+1e-10:
                    canceled.append(dict(time_ms=t,time_kst=clock(t),signal_ms=order['available'],
                        signal_kst=clock(order['available']),signal_price=order['reference'],reference=op,
                        assumed_fill=fill,chase_pct=chase_pct,cap_pct=cap,
                        limit_price=order['reference']*(1+cap/100),initial_stop=order['initial_stop'],cash= cash))
                else:
                    cost=cash;qty=cost/(fill*(1+fee));cash=0.;cycle+=1
                    position=dict(cycle=cycle,entry_ms=t,entry_kst=clock(t),entry_reference=op,
                                  cost=cost,quantity=qty,initial_stop=order['initial_stop'])
                    trades.append(dict(cycle=cycle,side='BUY',time_ms=t,time_kst=clock(t),reference=op,
                        assumed_fill=fill,quantity=qty,fee_krw=qty*fill*fee,cash_flow=-cost,
                        cash_after=cash,signal_available_ms=order['available'],
                        execution_bucket_delay_minutes=(t-order['available'])/60000,
                        initial_stop=order['initial_stop'],reason=order['reason'],
                        signal_price=order['reference'],chase_pct=chase_pct))
            else:
                assert position is not None
                fill=op*(1-slip);qty=position['quantity'];sale_fee=qty*fill*fee
                receipts=qty*fill-sale_fee;pnl=receipts-position['cost'];cash+=receipts;realized+=pnl
                trades.append(dict(cycle=cycle,side='SELL',time_ms=t,time_kst=clock(t),reference=op,
                    assumed_fill=fill,quantity=qty,fee_krw=sale_fee,cash_flow=receipts,
                    cash_after=cash,signal_available_ms=order['available'],
                    execution_bucket_delay_minutes=(t-order['available'])/60000,
                    realized_pnl=pnl,reason=order['reason']))
                cycles.append(dict(cycle=cycle,entry_kst=position['entry_kst'],entry_reference=position['entry_reference'],
                    entry_cost=position['cost'],exit_kst=clock(t),exit_reference=op,net_pnl=pnl,
                    net_pct=(receipts/position['cost']-1)*100,exit_reason=order['reason']))
                position=None;last_exit=t
        qty=position['quantity'] if position else 0.;cost=position['cost'] if position else 0.
        value=cash+qty*close
        assert math.isclose(value-initial,realized+qty*close-cost,abs_tol=1e-6)
        path.append(dict(time_ms=t+STEP,price=close,cash=cash,quantity=qty,equity=value,
                         realized_pnl=realized,unrealized_pnl=qty*close-cost))
    final=dict(path[-1]);final['quote_time_kst']=clock(final['time_ms'])
    final['marked_return_pct']=(final['equity']/initial-1)*100
    final['liquidation_equity']=final['cash']+final['quantity']*final['price']*(1-slip)*(1-fee)
    final['liquidation_return_pct']=(final['liquidation_equity']/initial-1)*100
    peak=initial;dd=0.
    for x in path:peak=max(peak,x['equity']);dd=max(dd,1-x['equity']/peak)
    final['marked_drawdown_pct']=dd*100
    return dict(trades=trades,closed_cycles=cycles,open_position=position,pending_order=pending,
                entry_decisions=decisions,final=final,path=path,canceled=canceled)


def conditional_cycle(rows,events,cancel,p,delay):
    future=[]
    for e in events:
        if e['available']<cancel['signal_ms']:continue
        e=dict(e)
        if e['kind']=='HOUR':e['buy_candidate']=e['available']==cancel['signal_ms']
        future.append(e)
    trial=run([r for r in rows if r[0]>=cancel['time_ms']],future,p,None,delay)
    assert sum(t['side']=='BUY' for t in trial['trades'])==1
    return dict(return_pct=trial['final']['liquidation_return_pct'],
                exit=trial['closed_cycles'][0] if trial['closed_cycles'] else None,
                still_open=trial['open_position'] is not None,terminal_price=trial['final']['price'])


def portfolio(paths,p,start,end):
    latest={m:float(p['capital_per_asset_krw']) for m in paths};cursor={m:0 for m in paths}
    peak=p['total_initial_capital_krw'];dd=0.
    for t in range(start+STEP,end+1,STEP):
        for m,path in paths.items():
            while cursor[m]<len(path) and path[cursor[m]]['time_ms']<=t:
                latest[m]=path[cursor[m]]['equity'];cursor[m]+=1
        value=sum(latest.values());peak=max(peak,value);dd=max(dd,1-value/peak)
    return dd*100


def checks():
    p=dict(capital_per_asset_krw=1000,fee_per_side=0,slippage_per_side=0)
    e=lambda t:dict(kind='HOUR',available=t,priority=1,buy_candidate=True,initial_stop=90,close=100,day_ms=t//DAY*DAY)
    rows=[[t,v,v,1,v,1,1] for t,v in [(0,101),(STEP,99),(DAY,100),(DAY+STEP,200)]]
    a=run(rows,[e(0)],p,.5)
    assert len(a['canceled'])==1 and not a['trades'],'NO_INTRABAR_OR_SAME_SIGNAL_RETRY'
    b=run(rows,[e(0),e(DAY)],p,.5)
    assert len(b['canceled'])==1 and b['trades'][0]['time_ms']==DAY,'FRESH_REENTRY_REQUIRED'
    assert run(rows,[e(0)],p,1)['trades'][0]['reference']==101,'EXACT_CAP_ALLOWED'
    delayed=run(rows,[e(0)],p,.5,5)
    assert delayed['trades'][0]['reference']==99 and delayed['trades'][0]['time_ms']==STEP
    costly=dict(p,slippage_per_side=.01)
    assert len(run([[0,100,100,100,100,1,1]],[e(0)],costly,.5)['canceled'])==1,'SLIPPAGE_COUNTS_IN_CAP'


def main():
    policy=json.loads((ROOT/'policy.json').read_text());p=json.loads((SOURCE/'policy.json').read_text())
    frozen=json.loads((SOURCE/'results.json').read_text())
    start=ms(p['start_utc']);end=ms(p['cutoff_utc']);checks()
    source={};validations={}
    for market in p['markets']:
        daily=read_gzip(SOURCE/'data'/(market+'_daily.json.gz'));rows=read_gzip(SOURCE/'data'/(market+'_5m.json.gz'))
        checked=validate_data(daily,rows,start,end);_,hourly=build_features(daily,rows,end)
        events=make_events(daily,hourly,start,end,100)
        a=original_run(rows,events,p);b=run(rows,events,p)
        assert a['final']==b['final'] and a['path']==b['path'] and a['closed_cycles']==b['closed_cycles']
        assert b['final']==frozen['assets'][market]['final']
        assert [{k:t[k] for k in old} for t,old in zip(b['trades'],a['trades'])]==a['trades']
        source[market]=(rows,events);validations[market]=dict(daily_ohlcv_matches=checked,baseline_identical=True)
    scenarios=[]
    for delay in policy['delay_minutes']:
        baseline=None
        for cap in [None,*policy['thresholds_pct']]:
            assets={};paths={}
            for market,(rows,events) in source.items():
                r=run(rows,events,p,cap,delay);paths[market]=r.pop('path')
                for cancel in r['canceled']:cancel['conditional_cycle']=conditional_cycle(rows,events,cancel,p,delay)
                # Remove all future candles/events, and all fill-candle fields except the open.
                for decision in [*r['trades'],*r['canceled']]:
                    t=decision['time_ms'];prefix=[x[:] for x in rows if x[0]<=t]
                    prefix[-1][2:5]=[prefix[-1][1]]*3;prefix[-1][5:]=[0,0]
                    z=run(prefix,[e for e in events if e['available']<=t],p,cap,delay)
                    assert z['trades']==[x for x in r['trades'] if x['time_ms']<=t]
                    assert z['canceled']==[{k:v for k,v in x.items() if k!='conditional_cycle'} for x in r['canceled'] if x['time_ms']<=t]
                r['name']=frozen['assets'][market]['name'];assets[market]=r
            equity=sum(a['final']['liquidation_equity'] for a in assets.values())
            summary=dict(delay_minutes=delay,cap_pct=cap,equity=equity,return_pct=(equity/p['total_initial_capital_krw']-1)*100,
                buys=sum(sum(t['side']=='BUY' for t in a['trades']) for a in assets.values()),
                cancellations=sum(len(a['canceled']) for a in assets.values()),
                marked_drawdown_pct=portfolio(paths,p,start,end),
                missed_winning_cycles=sum(c['conditional_cycle']['return_pct']>0 for a in assets.values() for c in a['canceled']),
                avoided_losing_cycles=sum(c['conditional_cycle']['return_pct']<0 for a in assets.values() for c in a['canceled']),
                open_assets=[m for m,a in assets.items() if a['open_position']])
            if cap is None:baseline=dict(summary)
            summary['delta_equity_vs_same_delay']=equity-baseline['equity']
            summary['delta_pp_vs_same_delay']=summary['return_pct']-baseline['return_pct']
            scenario=dict(summary=summary,assets=assets)
            scenarios.append(scenario);print(json.dumps(summary,ensure_ascii=False),flush=True)
    out=dict(policy=policy,checks=validations,scenarios=scenarios)
    (ROOT/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
