"""Compare exits at one causal Sep18 entry, without assuming a future sale."""
from datetime import datetime
import json
import math
from pathlib import Path

from research.ark_target_cycles.review import ROOT,SOURCE,STEP,HOUR,clock,build_features
from research.september_exit_validation.replay import read_gzip,daily_states,simulate


def main():
    scope=json.loads((ROOT/'scope.json').read_text())
    hold_scope=json.loads((ROOT/'hold_scope.json').read_text())
    cutoff=int(datetime.fromisoformat(scope['data_cutoff_kst']).timestamp()*1000)
    daily=read_gzip(SOURCE/'daily.json.gz');rows=read_gzip(SOURCE/'minutes5.json.gz')
    _,hourly=build_features(daily,rows,cutoff)
    signal=next(p for p in hourly if p['day']=='09/18' and p['buy_confirmed'])
    fill=next(r for r in rows if r[0]>=signal['observed_ms'])
    entry=fill[0];price=fill[1];initial_stop=signal['provisional_daily_low']
    states=daily_states(daily)
    signals=[]
    for available,state in states.items():
        if entry<available<=cutoff and state['down']>=2 and state['price_break']:
            signals.append(dict(available_ms=available,reason='DAILY_DECLINE_AND_PREVIOUS_LOW_BREAK'))
    for p in hourly:
        if entry<p['observed_ms']<=cutoff and p['hour_close']<initial_stop:
            signals.append(dict(available_ms=p['observed_ms'],reason='HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE'))
    signals.sort(key=lambda s:s['available_ms'])
    sell=None;pending=None
    if signals:
        s=signals[0];exit_bar=next((r for r in rows if r[0]>=s['available_ms']),None)
        if exit_bar:
            sell=dict(signal=s,time_ms=exit_bar[0],time_kst=clock(exit_bar[0]),price=exit_bar[1])
        else:
            pending=s
    fee=hold_scope['fee_per_side'];slip=hold_scope['slippage_per_side'];capital=hold_scope['initial_capital_krw']
    qty=capital/(price*(1+slip)*(1+fee));mark=rows[-1][4]
    value=qty*mark;liquidation=value*(1-slip)*(1-fee)
    old=json.loads((ROOT.parent/'september_exit_validation/policy.json').read_text())
    controls={m:simulate(rows,states,entry,rows[-1][0],m,old) for m in ('A','C')}
    assert all(o['exit_on_signal'] for o in controls.values())
    control_net=sum(o['net_pct'] for o in controls.values())/2
    peak=price;mdd=0;peak_at=entry;drawdown_pair=None
    for r in rows:
        if r[0]<entry:
            continue
        if r[4]>peak:
            peak=r[4];peak_at=r[0]+STEP
        drawdown=1-r[4]/peak
        if drawdown>mdd:
            mdd=drawdown;drawdown_pair=dict(peak_price=peak,peak_kst=clock(peak_at),trough_price=r[4],trough_kst=clock(r[0]+STEP))
    latest_daily=daily[-1]
    result=dict(scope=hold_scope,data_cutoff_kst=scope['data_cutoff_kst'],
        entry=dict(signal_kst=signal['observed_kst'],fill_kst=clock(entry),price=price,
                   initial_structure_stop=initial_stop,quantity=qty),
        actual_simulated_exit=sell,pending_exit=pending,
        current=dict(price=mark,marked_equity=value,marked_return_pct=(value/capital-1)*100,
                     hypothetical_liquidation_equity=liquidation,hypothetical_liquidation_pct=(liquidation/capital-1)*100,
                     realized_pnl=0 if sell is None else None,
                     last_completed_daily_label=clock(latest_daily[0])[:5],
                     previous_daily_low_for_current_daily_exit=latest_daily[3]),
        controls={m:dict(exit_kst=clock(o['exit_ms']),price=o['exit_reference'],net_pct=o['net_pct']) for m,o in controls.items()},
        split_control_net_pct=control_net,close_to_close_drawdown_pct=mdd*100,drawdown_pair=drawdown_pair,
        future_exit_formula='net_return(P) = (P / entry_reference) * (1-slip)*(1-fee)/((1+slip)*(1+fee)) - 1',
        limits=['Conditional Sep18 entry only, not a fully validated reentry strategy.',
                'Current liquidation is hypothetical, not a future Sep27 sale.',
                'Whole-hour close confirmation can miss intra-hour breaks; drawdown metric uses five-minute closes.'])
    assert sell is None,'This report renderer currently expects the observed open-position case.'
    assert math.isclose(liquidation/capital,mark/price*(1-slip)*(1-fee)/((1+slip)*(1+fee)),rel_tol=1e-12)
    (ROOT/'hold_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
