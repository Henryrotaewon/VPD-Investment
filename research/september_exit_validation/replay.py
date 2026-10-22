"""Offline, causal daily/5-minute exit comparison. No parameter fitting/orders."""
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from magi2.fast_wave_indicator import Candle, DAY_MS, indicators

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'
METHODS=('A','B','C','D')
KEYS=('macd_hist','rsi','williams')
KST=ZoneInfo('Asia/Seoul')


def clock(ms):
    return datetime.fromtimestamp(ms/1000,KST).strftime('%m/%d %H:%M')


def read_gzip(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def daily_states(raw):
    bars=[Candle(r[0],r[2],r[3],r[4],r[5]) for r in raw]
    values={}
    result={}
    for i in range(34,len(bars)):
        values[i]=indicators(bars[:i+1])
        if i-1 not in values:
            continue
        velocity={k:values[i][k]-values[i-1][k] for k in KEYS}
        result[raw[i][0]+DAY_MS]=dict(
            values=values[i],velocity=velocity,
            down=sum(v < -1e-10 for v in velocity.values()),
            price_break=raw[i][4] < raw[i-1][3])
    return result


def atr_values(rows,period):
    trs=[]; result=[]; previous=None
    for i,r in enumerate(rows):
        tr=r[2]-r[3] if i==0 else max(r[2]-r[3],abs(r[2]-rows[i-1][4]),abs(r[3]-rows[i-1][4]))
        trs.append(tr)
        if i+1 < period:
            result.append(None)
            continue
        previous=sum(trs[:period])/period if i+1==period else (previous*(period-1)+tr)/period
        result.append(previous)
    return result


def simulate(rows,daily,entry_ms,end_ms,method,policy):
    interval=policy['bar_minutes']*60000
    entry_index=next(i for i,r in enumerate(rows) if r[0]==entry_ms)
    end_index=next(i for i,r in enumerate(rows) if r[0]==end_ms)
    entry=rows[entry_index][1]
    peak=entry; active=False; trail=None; decision=None
    atr=atr_values(rows,policy['D']['atr_period']) if method=='D' else None
    if method in ('A','B'):
        for boundary,state in sorted(daily.items()):
            if not entry_ms < boundary <= end_ms:
                continue
            if state['down']>=2 and (method=='A' or state['price_break']):
                fill=next(r for r in rows if r[0]>=boundary)
                peak=max([entry,fill[1]]+[r[2] for r in rows if entry_ms<=r[0]<boundary])
                reason='DAILY_INDICATOR_DECLINE' if method=='A' else 'DAILY_DECLINE_AND_PRICE_BREAK'
                decision=dict(signal_bar_open_ms=boundary-DAY_MS,signal_available_ms=boundary,
                              exit_ms=fill[0],exit_reference=fill[1],reason=reason,detail=state)
                break
    for i in range(entry_index,end_index) if method in ('C','D') else ():
        r=rows[i]; boundary=r[0]+interval
        assert rows[i+1][0]>=boundary
        peak=max(peak,r[2])  # Only after this candle is complete.
        reason=None; detail={}
        if method in ('A','B'):
            state=daily.get(boundary)
            if state and state['down']>=2 and (method=='A' or state['price_break']):
                reason='DAILY_INDICATOR_DECLINE' if method=='A' else 'DAILY_DECLINE_AND_PRICE_BREAK'
                detail=state
        else:
            cfg=policy[method]
            active=active or peak>=entry*(1+cfg['activation_gross_pct']/100)
            if active:
                proposed=peak*(1-cfg['trailing_drawdown_pct']/100) if method=='C' else peak-cfg['atr_multiple']*atr[i-1]
                trail=proposed if trail is None else max(trail,proposed)
                if r[4]<=trail:
                    reason='CLOSE_BELOW_FIXED_TRAIL' if method=='C' else 'CLOSE_BELOW_PRIOR_ATR_TRAIL'
                    detail=dict(trail=trail,closed_bar_close=r[4],peak_known=peak,
                                atr_prior=atr[i-1] if atr else None)
        if reason:
            decision=dict(signal_bar_open_ms=r[0],signal_available_ms=boundary,
                          exit_ms=rows[i+1][0],exit_reference=rows[i+1][1],reason=reason,detail=detail)
            break
    if decision is None:
        peak=max([entry]+[r[2] for r in rows if entry_ms<=r[0]<end_ms])
        decision=dict(signal_bar_open_ms=None,signal_available_ms=end_ms,exit_ms=end_ms,
                      exit_reference=rows[end_index][1],reason='STUDY_ENDPOINT',detail={})
    sold=decision['exit_reference']; seen_peak=max(peak,sold)
    fee=policy['fee_per_side']; slip=policy['slippage_per_side']
    ratio=sold/entry
    decision.update(method=method,entry_ms=entry_ms,entry_reference=entry,
                    gross_pct=(ratio-1)*100,
                    net_pct=(ratio*(1-slip)*(1-fee)/((1+slip)*(1+fee))-1)*100,
                    hours_held=(decision['exit_ms']-entry_ms)/3600000,
                    execution_gap_minutes=(decision['exit_ms']-decision['signal_available_ms'])/60000,
                    peak_seen=seen_peak,giveback_seen_pct=(1-sold/seen_peak)*100,
                    exit_on_signal=decision['reason']!='STUDY_ENDPOINT')
    return decision


def validate(rows,daily,entry,end,policy):
    interval=policy['bar_minutes']*60000
    if any(b[0]<=a[0] or (b[0]-a[0])%interval for a,b in zip(rows,rows[1:])):
        raise ValueError('INVALID_CANDLE_ORDER_OR_INTERVAL')
    assert any(r[0]==entry for r in rows) and any(r[0]==end for r in rows)
    for r in rows:
        assert all(math.isfinite(x) for x in r)
        assert r[0]%interval==0 and 0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2] and r[5]>=0
    by_day={r[0]:r for r in daily}
    for stamp in range(entry,end,DAY_MS):
        block=[r for r in rows if stamp<=r[0]<stamp+DAY_MS]
        assert 0<len(block)<=DAY_MS//interval
        d=by_day[stamp]
        observed=[block[0][1],max(r[2] for r in block),min(r[3] for r in block),block[-1][4]]
        assert all(math.isclose(x,y,rel_tol=1e-9,abs_tol=1e-10) for x,y in zip(observed,d[1:5])), 'DAILY_MINUTE_OHLC_MISMATCH'
        assert math.isclose(sum(r[5] for r in block),d[5],rel_tol=1e-7,abs_tol=1e-6), 'DAILY_MINUTE_VOLUME_MISMATCH'


def main():
    policy=json.loads((ROOT/'policy.json').read_text())
    execution_notes=json.loads((ROOT/'execution_notes.json').read_text())
    manifest=json.loads((DATA/'selection.json').read_text())
    assert manifest['policy']==policy
    daily_all=read_gzip(DATA/'daily_all.json.gz')
    # Reproduce the sample ranking from archived histories, not selected outcomes.
    from research.september_exit_validation.collect import signals
    recomputed=[]
    excluded=set(policy['excluded_assets'])
    for symbol,rows in daily_all.items():
        if symbol.split('-',1)[1] not in excluded:
            recomputed.extend(signals(symbol,rows))
    recomputed.sort(key=lambda r:(r['entry_ms'],-r['median_turnover_krw'],r['market']))
    seen=set(); selected=[]
    for event in recomputed:
        if event['market'] not in seen:
            selected.append(event);seen.add(event['market'])
        if len(selected)==policy['sample_size']:
            break
    assert selected==manifest['selected']
    ark_ms=int(datetime.fromisoformat(policy['ark_reference_entry']).replace(tzinfo=timezone.utc).timestamp()*1000)
    cases=[dict(market='KRW-ARK',entry_ms=ark_ms,reference_only=True)]+selected
    results=[]; invalid=[]
    names={r['market']:r['korean_name'] for r in manifest['current_krw_markets']}
    for event in cases:
        symbol=event['market'];entry=event['entry_ms'];end=entry+policy['evaluation_days']*DAY_MS
        rows=read_gzip(DATA/(symbol+'_5m.json.gz'))
        raw=daily_all[symbol]
        try:
            validate(rows,raw,entry,end,policy)
        except (ValueError,AssertionError) as e:
            invalid.append(dict(market=symbol,error=str(e)));continue
        states=daily_states(raw)
        reference=next(r[1] for r in rows if r[0]==entry)
        held_rows=[r for r in rows if entry<=r[0]<end]
        end_open=next(r[1] for r in rows if r[0]==end)
        window_peak=max(max(r[2] for r in held_rows),end_open)
        methods={m:simulate(rows,states,entry,end,m,policy) for m in METHODS}
        # Truncate every future 5m and daily observation after each exit. A
        # decision must match; keep only the exit bar OPEN, zero its future range.
        for m,outcome in methods.items():
            exit_ms=outcome['exit_ms']
            if exit_ms==end:
                continue
            prefix=[r[:] for r in rows if r[0]<=exit_ms]
            prefix[-1][2:5]=[prefix[-1][1]]*3
            prefix[-1][5:]=[0,0]
            past_daily={k:v for k,v in states.items() if k<=exit_ms}
            earlier=simulate(prefix,past_daily,entry,exit_ms,m,policy)
            assert earlier==outcome, 'FUTURE_DATA_CHANGED_DECISION'
        for outcome in methods.values():
            outcome['gap_from_full_window_peak_pct']=(1-outcome['exit_reference']/window_peak)*100
            outcome['within_5pct_full_window_peak']=outcome['exit_reference']>=window_peak*.95
            outcome['net_if_20bps_slip_each_side_pct']=(outcome['exit_reference']/reference*(1-.002)*(1-policy['fee_per_side'])/((1+.002)*(1+policy['fee_per_side']))-1)*100
        results.append(dict(market=symbol,name=names.get(symbol,symbol),entry_ms=entry,end_ms=end,
                            reference_only=event.get('reference_only',False),entry_reference=reference,
                            full_window_peak=window_peak,max_gross_opportunity_pct=(window_peak/reference-1)*100,
                            methods=methods))
    sample=[r for r in results if not r['reference_only']]
    summary={}
    for m in METHODS:
        vals=[r['methods'][m] for r in sample]
        summary[m]=dict(n=len(vals),mean_net_pct=mean(v['net_pct'] for v in vals),
                        median_net_pct=median(v['net_pct'] for v in vals),
                        profitable=sum(v['net_pct']>0 for v in vals),
                        signal_exits=sum(v['exit_on_signal'] for v in vals),
                        mean_seen_giveback_pct=mean(v['giveback_seen_pct'] for v in vals),
                        within_5pct_full_peak=sum(v['within_5pct_full_window_peak'] for v in vals),
                        better_than_A=sum(r['methods'][m]['net_pct']>r['methods']['A']['net_pct']+1e-9 for r in sample),
                        mean_delta_A_pp=mean(r['methods'][m]['net_pct']-r['methods']['A']['net_pct'] for r in sample),
                        mean_net_stressed_cost_pct=mean(v['net_if_20bps_slip_each_side_pct'] for v in vals))
    report=dict(mode='RESEARCH_ONLY',policy=policy,execution_notes=execution_notes,sample_n=len(sample),selection_count=len(selected),
                selection_reproduced=True,invalid_cases=invalid,source_failures=manifest['failures'],
                quality_checks=['DAILY_MINUTE_OHLC_AND_VOLUME_MATCH','NATIVE_GAPS_NO_SYNTHETIC_FILLS',
                                'FUTURE_TRUNCATION_INVARIANT','FROZEN_SELECTION_REPRODUCED'],
                cases=results,summary=summary,
                caveats=['10 correlated cases in one month are exploratory, not statistical proof.',
                         'ARK excluded from cross-asset summary.',
                         'Not an approved entry detector or executable strategy.',
                         'Current-list survivorship and data failure coverage limits.',
                         '5m next-open cost scenario; no historical order-book or latency reconstruction.',
                         'Full-window peak is hindsight comparison only, never a signal input.',
                         'Uniform endpoint exit is a study convention; no common protective stop.'])
    (ROOT/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(summary=summary,invalid=invalid,
                     cases=[dict(market=r['market'],entry=clock(r['entry_ms']),
                       results={m:dict(exit=clock(o['exit_ms']),price=o['exit_reference'],
                          net=round(o['net_pct'],3),reason=o['reason']) for m,o in r['methods'].items()}) for r in results]),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
