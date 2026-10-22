"""Causal ARK period replay, including fresh reentry and terminal inventory."""
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from magi2.fast_wave_indicator import Candle, DAY_MS, indicators
from research.september_exit_validation.replay import read_gzip, validate

ROOT=Path(__file__).resolve().parent
PRIOR=ROOT.parent/'september_exit_validation'
KST=ZoneInfo('Asia/Seoul')
STEP=300000


def ms(value):
    return int(datetime.fromisoformat(value).timestamp()*1000)


def clock(stamp):
    return datetime.fromtimestamp(stamp/1000,KST).strftime('%m/%d %H:%M')


def audit_daily(raw,policy,old):
    bars=[Candle(r[0],r[2],r[3],r[4],r[5]) for r in raw]
    start=ms(policy['window_start_utc']);end=ms(policy['completed_bar_cutoff_utc'])
    cfg=old['selection'];e=cfg['epsilon'];out=[];cache={}
    for i,row in enumerate(raw):
        if row[0]<start or row[0]+DAY_MS>end:
            continue
        assert i+1>=old['minimum_history']
        for j in (i-2,i-1,i):
            if j not in cache:
                cache[j]=indicators(bars[:j+1])
        a,b,c=(cache[j] for j in (i-2,i-1,i))
        velocity={k:c[k]-b[k] for k in c}
        accel={k:c[k]-2*b[k]+a[k] for k in c}
        turnover=median(r[6] for r in raw[i-20:i])
        volume_base=median(r[5] for r in raw[i-21:i-1])
        ratio=raw[i-1][5]/volume_base if volume_base>0 else 0
        window=bars[i-149:i+1]
        tests=dict(history=all(y.open_ms-x.open_ms==DAY_MS for x,y in zip(window,window[1:])),
            turnover=turnover>=cfg['prior20_median_daily_turnover_min_krw'],
            previous_volume=ratio>=cfg['previous_volume_over_prior20_median_min'],
            current_volume=row[5]<raw[i-1][5],macd_accel=accel['macd_hist']>e,
            rsi=velocity['rsi']>e and accel['rsi']>e,
            williams=velocity['williams']>e and accel['williams']>e)
        out.append(dict(signal_day=clock(row[0])[:5],open_ms=row[0],available_ms=row[0]+DAY_MS,
            close=row[4],entry_ok=all(tests.values()),tests=tests,
            down=sum(velocity[k]<-e for k in ('macd_hist','rsi','williams')),
            median_turnover_krw=turnover,prior_volume_ratio=ratio,values=c,velocity=velocity,acceleration=accel))
    return out


def run(rows,audits,policy):
    initial=policy['initial_capital_krw'];cash=float(initial);realized=0.
    fee=policy['fee_per_side'];slip=policy['slippage_per_side']
    first_entry=ms(policy['initial_entry_utc'])
    done_initial=not policy.get('assume_initial_entry',True)
    position=None;pending=None;last_exit=-1;cursor=0;cycle=0
    trades=[];closed=[];timeline=[];actions=[]
    for r in rows:
        t,op,hi,lo,close=r[:5]
        while cursor<len(audits) and audits[cursor]['available_ms']<=t:
            d=audits[cursor];cursor+=1
            action='NO_ENTRY_SIGNAL'
            if position and d['down']>=2:
                pending=dict(kind='SELL_ALL',available=d['available_ms'],reason='DAILY_INDICATOR_DECLINE')
                action='FULL_EXIT_SIGNAL'
            elif d['entry_ok']:
                if position:
                    action='IGNORE_ENTRY_WHILE_HOLDING'
                elif not done_initial:
                    action='INITIAL_CASE_NOT_YET_ENTERED'
                elif d['available_ms']>last_exit:
                    pending=dict(kind='BUY',available=d['available_ms'],reason='FRESH_DAILY_REENTRY' if cycle else 'DAILY_ENTRY')
                    action='REENTRY_SIGNAL'
                else:
                    action='IGNORE_OLD_OR_SAME_BOUNDARY_SIGNAL'
            if d['available_ms']==first_entry and not done_initial:
                action='USER_CASE_INITIAL_ENTRY_ASSUMED'
            actions.append(dict(signal_day=d['signal_day'],entry_ok=d['entry_ok'],down=d['down'],action=action))
        if not done_initial and t>=first_entry:
            pending=dict(kind='BUY',available=first_entry,reason='USER_CASE_INITIAL_ENTRY')
        if pending and pending['available']<=t:
            order=pending;pending=None
            if order['kind']=='BUY':
                assert position is None and cash>0
                cost=cash;fill=op*(1+slip);qty=cost/(fill*(1+fee));cash=0.
                cycle+=1;done_initial=True
                position=dict(cycle=cycle,entry_ms=t,entry_reference=op,original_cost=cost,
                    original_qty=qty,qty=qty,cost=cost,peak=op,active=False,partial_done=False,sales=[])
                trades.append(dict(cycle=cycle,side='BUY',time_ms=t,time_kst=clock(t),quantity=qty,
                    reference=op,assumed_fill=fill,fee_krw=qty*fill*fee,cash_flow=-cost,
                    cash_after=cash,quantity_after=qty,reason=order['reason'],signal_available_ms=order['available']))
            else:
                assert position is not None
                before=position['qty']
                qty=before if order['kind']=='SELL_ALL' else position['original_qty']*policy['partial_fraction']
                assert 0<qty<=before
                allocated=position['cost']*(qty/before)
                fill=op*(1-slip);sale_fee=qty*fill*fee;proceeds=qty*fill-sale_fee
                cash+=proceeds;realized+=proceeds-allocated
                position['qty']-=qty;position['cost']-=allocated
                position['partial_done']=True
                trade=dict(cycle=cycle,side='SELL',time_ms=t,time_kst=clock(t),quantity=qty,
                    original_quantity_fraction=qty/position['original_qty'],reference=op,assumed_fill=fill,
                    fee_krw=sale_fee,cash_flow=proceeds,cash_after=cash,quantity_after=position['qty'],
                    allocated_cost=allocated,realized_pnl=proceeds-allocated,reason=order['reason'],
                    signal_available_ms=order['available'])
                trades.append(trade);position['sales'].append(trade)
                if order['kind']=='SELL_ALL':
                    receipts=sum(s['cash_flow'] for s in position['sales'])
                    closed.append(dict(cycle=cycle,entry_ms=position['entry_ms'],entry_reference=position['entry_reference'],
                        exit_ms=t,entry_cost=position['original_cost'],proceeds=receipts,
                        net_pnl=receipts-position['original_cost'],net_pct=(receipts/position['original_cost']-1)*100,
                        weighted_exit_reference=sum(s['quantity']*s['reference'] for s in position['sales'])/position['original_qty']))
                    position=None;last_exit=t
        if position:
            position['peak']=max(position['peak'],hi)
            position['active']=position['active'] or position['peak']>=position['entry_reference']*(1+policy['activation_gross_pct']/100)
            if position['active'] and not position['partial_done'] and close<=position['peak']*(1-policy['trailing_drawdown_pct']/100):
                pending=dict(kind='SELL_HALF',available=t+STEP,reason='FIXED_TRAIL_PARTIAL')
        qty=position['qty'] if position else 0.
        cost=position['cost'] if position else 0.
        equity=cash+qty*close
        assert math.isclose(equity-initial,realized+qty*close-cost,abs_tol=1e-6)
        timeline.append(dict(time_ms=t+STEP,price=close,cash=cash,quantity=qty,remaining_cost=cost,
                             realized_pnl=realized,unrealized_pnl=qty*close-cost,equity=equity,
                             cumulative_pct=(equity/initial-1)*100))
    assert timeline
    final=dict(timeline[-1]);final['time_kst']=clock(final['time_ms'])
    final['net_liquidation_equity']=final['cash']+final['quantity']*final['price']*(1-slip)*(1-fee)
    final['net_liquidation_pct']=(final['net_liquidation_equity']/initial-1)*100
    return dict(trades=trades,closed_cycles=closed,open_position=position,final=final,
                pending_order=pending,daily_actions=actions,timeline=timeline)


def check_synthetic():
    p=dict(initial_capital_krw=1000,fee_per_side=0,slippage_per_side=0,
           initial_entry_utc='1970-01-01T00:00:00+00:00',partial_fraction=.5,
           activation_gross_pct=12,trailing_drawdown_pct=5)
    rows=[[i*DAY_MS,x,x,x,x,1,1] for i,x in enumerate((100,110,90,95,95))]
    audits=[dict(available_ms=i*DAY_MS,entry_ok=True,down=2 if i in (1,3) else 0,signal_day=str(i)) for i in (1,2,3)]
    result=run(rows,audits,p)
    assert [t['side'] for t in result['trades']]==['BUY','SELL','BUY','SELL']
    assert [t['time_ms'] for t in result['trades']]==[i*DAY_MS for i in range(4)]
    assert math.isclose(result['final']['cash'],1000*1.1*95/90)
    assert result['final']['quantity']==0 and len(result['closed_cycles'])==2


def main():
    policy=json.loads((ROOT/'policy.json').read_text())
    old=json.loads((PRIOR/'policy.json').read_text())
    manifest=json.loads((ROOT/'data/manifest.json').read_text())
    assert policy==manifest['policy']
    rows=read_gzip(ROOT/'data/minutes5.json.gz');daily=read_gzip(ROOT/'data/daily.json.gz')
    start=ms(policy['window_start_utc']);end=ms(policy['completed_bar_cutoff_utc'])
    last_day=end//DAY_MS*DAY_MS
    validate(rows,daily,start,last_day,old)
    assert rows[-1][0]+STEP==end and all(r[0]+STEP<=end for r in rows)
    assert all(d[0]+DAY_MS<=end for d in daily)
    previous=read_gzip(PRIOR/'data/KRW-ARK_5m.json.gz')
    available={r[0]:r for r in rows}
    common=[r for r in previous if r[0] in available]
    assert common and all(r==available[r[0]] for r in common),'ARCHIVE_OVERLAP_CHANGED'
    audit=audit_daily(daily,policy,old)
    # Verify that the detector is identical to the old study on its old date range.
    from research.september_exit_validation.collect import signals
    old_events={e['entry_ms'] for e in signals('KRW-ARK',daily)}
    for a in audit:
        if a['signal_day']<='09/19':
            assert a['entry_ok']==(a['available_ms'] in old_events)
    check_synthetic()
    sensitivity=json.loads((ROOT/'pattern_sensitivity.json').read_text())
    pattern=[dict(a,entry_ok=all(v for k,v in a['tests'].items() if k!='turnover')) for a in audit]
    uniform=dict(policy,assume_initial_entry=False)
    scenarios={}
    for name,inputs,p in [('assumed_initial_case',audit,policy),('strict_uniform',audit,uniform),('indicator_pattern',pattern,uniform)]:
        result=run(rows,inputs,p)
        for trade in result['trades']:
            prefix=[r[:] for r in rows if r[0]<=trade['time_ms']]
            prefix[-1][2:5]=[prefix[-1][1]]*3;prefix[-1][5:]=[0,0]
            past=[a for a in inputs if a['available_ms']<=trade['time_ms']]
            short=run(prefix,past,p)
            assert short['trades']==[t for t in result['trades'] if t['time_ms']<=trade['time_ms']]
        marks=[]
        for boundary in list(range(start+DAY_MS,end,DAY_MS))+[end]:
            past=[t for t in result['timeline'] if t['time_ms']<=boundary]
            if past:
                marks.append(dict(past[-1],evaluation_ms=boundary,evaluation_kst=clock(boundary)))
        result.pop('timeline');result['daily_marks']=marks
        scenarios[name]=result
    report=dict(policy=policy,sensitivity_policy=sensitivity,audit=audit,scenarios=scenarios,
        checks=['14_FULL_DAYS_OHLCV_MATCH','PRIOR_ARCHIVE_OVERLAP_IDENTICAL',
                'OLD_DETECTOR_CRITERIA_REPRODUCED','FUTURE_TRUNCATION_INVARIANT',
                'CASH_AND_QUANTITY_ACCOUNTING','FRESH_REENTRY_AND_SAME_BOUNDARY_PRIORITY'])
    (ROOT/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({n:dict(trades=r['trades'],closed_cycles=r['closed_cycles'],final=r['final'],pending=r['pending_order']) for n,r in scenarios.items()},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
