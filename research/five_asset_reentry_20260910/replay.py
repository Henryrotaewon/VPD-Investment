"""Identical causal reentry/holding rules, five independent capital sleeves."""
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from research.ark_target_cycles.review import build_features
from research.september_exit_validation.replay import read_gzip,daily_states

ROOT=Path(__file__).resolve().parent
KST=ZoneInfo('Asia/Seoul')
DAY=86400000
STEP=300000


def ms(value):
    return int(datetime.fromisoformat(value).timestamp()*1000)


def clock(stamp):
    return datetime.fromtimestamp(stamp/1000,KST).strftime('%m/%d %H:%M')


def validate_data(daily,rows,start,end):
    assert rows and all(r[0]+DAY<=end for r in daily)
    assert all(b[0]-a[0]==DAY for a,b in zip(daily,daily[1:]))
    assert all(b[0]>a[0] and (b[0]-a[0])%STEP==0 for a,b in zip(rows,rows[1:]))
    for r in rows:
        assert start<=r[0] and r[0]+STEP<=end and r[0]%STEP==0
        assert all(math.isfinite(x) for x in r)
        assert 0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2] and r[5]>=0
    by_day={r[0]:r for r in daily};checked=0
    for t in range(start,end//DAY*DAY,DAY):
        block=[r for r in rows if t<=r[0]<t+DAY]
        assert block
        actual=[block[0][1],max(r[2] for r in block),min(r[3] for r in block),block[-1][4]]
        assert all(math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-10) for a,b in zip(actual,by_day[t][1:5]))
        assert math.isclose(sum(r[5] for r in block),by_day[t][5],rel_tol=1e-7,abs_tol=1e-6)
        checked+=1
    return checked


def make_events(daily,hourly,start,end,minimum_history):
    events=[];seen=set()
    for t,s in daily_states(daily).items():
        if start<t<=end and s['down']>=2 and s['price_break']:
            events.append(dict(available=t,priority=0,kind='DAILY_EXIT',down=s['down']))
    for p in hourly:
        history=sum(r[0]+DAY<=p['observed_ms'] for r in daily)
        first=p['buy_confirmed'] and p['day_ms'] not in seen and history>=minimum_history
        if first:
            seen.add(p['day_ms'])
        events.append(dict(available=p['observed_ms'],priority=1,kind='HOUR',
            buy_candidate=first,day_ms=p['day_ms'],close=p['hour_close'],
            initial_stop=p['provisional_daily_low']))
    return sorted(events,key=lambda e:(e['available'],e['priority']))


def run(rows,events,p):
    initial=p['capital_per_asset_krw'];cash=float(initial);realized=0.
    fee=p['fee_per_side'];slip=p['slippage_per_side']
    position=None;pending=None;cursor=0;cycle=0;last_exit=-1
    trades=[];cycles=[];path=[];decisions=[]
    for r in rows:
        t,op,hi,lo,close=r[:5]
        while cursor<len(events) and events[cursor]['available']<=t:
            e=events[cursor];cursor+=1
            reason=None
            if position:
                if e['kind']=='DAILY_EXIT':
                    reason='DAILY_WEAKNESS_AND_PREVIOUS_LOW_BREAK'
                elif e['kind']=='HOUR' and e['available']>position['entry_ms'] and e['close']<position['initial_stop']:
                    reason='HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE'
            if reason:
                order=dict(side='SELL',available=e['available'],priority=e['priority'],reason=reason)
                if pending is None or pending['side']=='BUY' or (order['available'],order['priority'])<(pending['available'],pending['priority']):
                    pending=order
            if e['kind']=='HOUR' and e['buy_candidate']:
                if position:
                    status='IGNORED_WHILE_HOLDING'
                elif pending is not None or e['available']<=last_exit:
                    status='IGNORED_PENDING_OR_STALE'
                else:
                    pending=dict(side='BUY',available=e['available'],priority=2,
                        initial_stop=e['initial_stop'],reason='FRESH_TWO_HOUR_DAILY_RECOVERY')
                    status='BUY_SCHEDULED'
                decisions.append(dict(signal_kst=clock(e['available']),day_kst=clock(e['day_ms'])[:5],status=status))
        if pending is not None and pending['available']<=t:
            order=pending;pending=None
            if order['side']=='BUY':
                assert position is None and cash>0
                cost=cash;fill=op*(1+slip);qty=cost/(fill*(1+fee));cash=0.
                cycle+=1
                position=dict(cycle=cycle,entry_ms=t,entry_kst=clock(t),entry_reference=op,
                              cost=cost,quantity=qty,initial_stop=order['initial_stop'])
                trades.append(dict(cycle=cycle,side='BUY',time_ms=t,time_kst=clock(t),reference=op,
                    assumed_fill=fill,quantity=qty,fee_krw=qty*fill*fee,cash_flow=-cost,
                    cash_after=cash,signal_available_ms=order['available'],
                    execution_bucket_delay_minutes=(t-order['available'])/60000,
                    initial_stop=order['initial_stop'],reason=order['reason']))
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
    for x in path:
        peak=max(peak,x['equity']);dd=max(dd,1-x['equity']/peak)
    final['marked_drawdown_pct']=dd*100
    return dict(trades=trades,closed_cycles=cycles,open_position=position,pending_order=pending,
                entry_decisions=decisions,final=final,path=path)


def synthetic_check():
    p=dict(capital_per_asset_krw=1000,fee_per_side=0,slippage_per_side=0)
    rows=[[t,x,x,x,x,1,1] for t,x in [(0,100),(DAY,80),(2*DAY,90),(2*DAY+3600000,85)]]
    events=[dict(kind='HOUR',available=0,priority=1,buy_candidate=True,initial_stop=90,close=100,day_ms=0),
        dict(kind='DAILY_EXIT',available=DAY,priority=0),
        dict(kind='HOUR',available=DAY,priority=1,buy_candidate=True,initial_stop=70,close=80,day_ms=DAY),
        dict(kind='HOUR',available=2*DAY,priority=1,buy_candidate=True,initial_stop=89,close=90,day_ms=2*DAY),
        dict(kind='HOUR',available=2*DAY+3600000,priority=1,buy_candidate=False,initial_stop=80,close=88,day_ms=2*DAY)]
    a=run(rows,events,p)
    assert [t['side'] for t in a['trades']]==['BUY','SELL','BUY','SELL']
    assert a['trades'][-1]['reference']==85  # adverse gap, not the stop at 89
    assert math.isclose(a['final']['cash'],1000*.8*85/90)


def main():
    p=json.loads((ROOT/'policy.json').read_text());manifest=json.loads((ROOT/'data/manifest.json').read_text())
    revision=json.loads((ROOT/'warmup_revision.json').read_text())
    assert p==manifest['policy'] and revision==manifest['warmup_revision']
    start=ms(p['start_utc']);end=ms(p['cutoff_utc'])
    synthetic_check();assets={};paths={};checks={}
    names={m['market']:m['name'] for m in manifest['markets']}
    for market in p['markets']:
        daily=read_gzip(ROOT/'data'/(market+'_daily.json.gz'))
        rows=read_gzip(ROOT/'data'/(market+'_5m.json.gz'))
        assert sum(r[0]<start for r in daily)>=revision['effective_minimum_completed_history']
        checked=validate_data(daily,rows,start,end)
        days,hourly=build_features(daily,rows,end)
        events=make_events(daily,hourly,start,end,revision['effective_minimum_completed_history'])
        result=run(rows,events,p)
        for trade in result['trades']:
            prefix=[r[:] for r in rows if r[0]<=trade['time_ms']]
            prefix[-1][2:5]=[prefix[-1][1]]*3;prefix[-1][5:]=[0,0]
            before=[e for e in events if e['available']<=trade['time_ms']]
            repeated=run(prefix,before,p)
            assert repeated['trades']==[t for t in result['trades'] if t['time_ms']<=trade['time_ms']]
        first=next((h for h in hourly if h['buy_confirmed']),None)
        if first:
            boundary=first['observed_ms']
            _,past=build_features([d for d in daily if d[0]+DAY<=boundary],
                                 [r for r in rows if r[0]+STEP<=boundary],boundary)
            assert past[-1]==first,'FEATURE_USED_FUTURE_DATA'
        if market=='KRW-ARK':
            old=read_gzip(ROOT.parent/'ark_period_20260911/data/minutes5.json.gz')
            mapped={r[0]:r for r in rows}
            assert all(mapped[r[0]]==r for r in old)
        paths[market]=result.pop('path')
        strict=run(rows,make_events(daily,hourly,start,end,revision['original_minimum_completed_history']),p)
        result.update(name=names[market],candidate_count=sum(e.get('buy_candidate',False) for e in events),
                      original_150_day_sensitivity=dict(entry_count=sum(t['side']=='BUY' for t in strict['trades']),final=strict['final']))
        result['tomorrow_scenarios']={str(move):dict(price=result['final']['price']*(1+move/100),
            equity=result['final']['cash']+result['final']['quantity']*result['final']['price']*(1+move/100)*(1-p['slippage_per_side'])*(1-p['fee_per_side'])) for move in p['tomorrow_price_scenarios_pct']}
        for s in result['tomorrow_scenarios'].values():
            s['return_pct']=(s['equity']/p['capital_per_asset_krw']-1)*100
        assets[market]=result;checks[market]=dict(daily_ohlcv_matches=checked,future_invariant_trades=len(result['trades']))
        print(market,'buys',sum(t['side']=='BUY' for t in result['trades']),'liq_return',round(result['final']['liquidation_return_pct'],4),'open',result['open_position'] is not None,flush=True)
    # Align native-candle valuations only. A carried price never creates a fill.
    cursors={m:0 for m in p['markets']}
    latest={m:dict(equity=float(p['capital_per_asset_krw']),cash=float(p['capital_per_asset_krw']),quantity=0.,price=None) for m in p['markets']}
    portfolio_path=[];daily_marks=[];peak=p['total_initial_capital_krw'];dd=0.
    for t in range(start+STEP,end+1,STEP):
        for market in p['markets']:
            seq=paths[market]
            while cursors[market]<len(seq) and seq[cursors[market]]['time_ms']<=t:
                latest[market]=seq[cursors[market]];cursors[market]+=1
        equity=sum(x['equity'] for x in latest.values());peak=max(peak,equity);dd=max(dd,1-equity/peak)
        if t%DAY==0 or t==end:
            daily_marks.append(dict(time_kst=clock(t),equity=equity,return_pct=(equity/p['total_initial_capital_krw']-1)*100,
                                   assets={m:dict(latest[m]) for m in p['markets']}))
    total=sum(a['final']['equity'] for a in assets.values())
    liq=sum(a['final']['liquidation_equity'] for a in assets.values())
    assert math.isclose(total,equity,abs_tol=1e-6)
    tomorrow={str(move):dict(equity=sum(a['tomorrow_scenarios'][str(move)]['equity'] for a in assets.values())) for move in p['tomorrow_price_scenarios_pct']}
    for s in tomorrow.values():
        s['return_pct']=(s['equity']/p['total_initial_capital_krw']-1)*100
    portfolio=dict(initial=p['total_initial_capital_krw'],marked_equity=total,marked_return_pct=(total/p['total_initial_capital_krw']-1)*100,
        liquidation_equity=liq,liquidation_return_pct=(liq/p['total_initial_capital_krw']-1)*100,
        cash=sum(a['final']['cash'] for a in assets.values()),realized_pnl=sum(a['final']['realized_pnl'] for a in assets.values()),
        unrealized_pnl=sum(a['final']['unrealized_pnl'] for a in assets.values()),marked_drawdown_pct=dd*100,
        open_assets=[m for m,a in assets.items() if a['open_position']],tomorrow_scenarios=tomorrow)
    strict_equity=sum(a['original_150_day_sensitivity']['final']['liquidation_equity'] for a in assets.values())
    portfolio['original_150_day_sensitivity']=dict(liquidation_equity=strict_equity,
        liquidation_return_pct=(strict_equity/p['total_initial_capital_krw']-1)*100)
    result=dict(policy=p,warmup_revision=revision,assets=assets,portfolio=portfolio,daily_marks=daily_marks,checks=checks)
    (ROOT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(portfolio,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
