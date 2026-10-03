"""Review user-labeled swing dates using past-only daily and hourly snapshots."""
from datetime import datetime
import json
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from magi2.fast_wave_indicator import Candle, indicators, DAY_MS
from research.september_exit_validation.replay import read_gzip

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'ark_period_20260911/data'
KST=ZoneInfo('Asia/Seoul')
HOUR=3600000
STEP=300000
KEYS=('macd_hist','rsi','williams')
EPS=1e-10


def clock(stamp):
    return datetime.fromtimestamp(stamp/1000,KST).strftime('%m/%d %H:%M')


def feature(values,previous,before):
    velocity={k:values[k]-previous[k] for k in KEYS}
    accel={k:values[k]-2*previous[k]+before[k] for k in KEYS}
    setup=all(previous[k]-before[k]<-EPS for k in ('rsi','williams'))
    recovery=all(velocity[k]>EPS for k in ('rsi','williams')) and accel['macd_hist']>EPS
    return dict(values=values,velocity=velocity,acceleration=accel,
                pullback_setup=setup,recovery=recovery,buy_hypothesis=setup and recovery)


def build_features(daily,minutes,cutoff):
    candles=[Candle(r[0],r[2],r[3],r[4],r[5]) for r in daily]
    values={i:indicators(candles[:i+1]) for i in range(34,len(candles))}
    start=minutes[0][0]
    days=[]
    for i,r in enumerate(daily):
        if r[0]<start or i-2 not in values:
            continue
        out=feature(values[i],values[i-1],values[i-2])
        out.update(day=clock(r[0])[:5],open_ms=r[0],available_ms=r[0]+DAY_MS,
            available_kst=clock(r[0]+DAY_MS),ohlc=r[1:5],volume=r[5],
            volume_vs_previous=r[5]/daily[i-1][5],volume_vs_prior20_median=r[5]/median(x[5] for x in daily[i-20:i]))
        days.append(out)
    hourly=[]
    for boundary in range((start//HOUR+1)*HOUR,cutoff+1,HOUR):
        day=(boundary-1)//DAY_MS*DAY_MS
        observed=[r for r in minutes if day<=r[0] and r[0]+STEP<=boundary]
        block=[r for r in observed if boundary-HOUR<=r[0]]
        if not observed or not block:
            continue
        completed=[c for c in candles if c.open_ms<day]
        previous=indicators(completed);before=indicators(completed[:-1])
        live=Candle(day,max(r[2] for r in observed),min(r[3] for r in observed),
                    observed[-1][4],sum(r[5] for r in observed))
        f=feature(indicators([*completed,live]),previous,before)
        f.update(day=clock(day)[:5],day_ms=day,observed_ms=boundary,observed_kst=clock(boundary),
            hour_open=block[0][1],hour_high=max(r[2] for r in block),hour_low=min(r[3] for r in block),
            hour_close=block[-1][4],hour_volume=sum(r[5] for r in block),native_bars=len(block),
            price_bar_close_ms=block[-1][0]+STEP,provisional_daily_high=live.high,
            provisional_daily_low=live.low,provisional_daily_volume=live.volume)
        if hourly:
            p=hourly[-1]
            contiguous=p['observed_ms']+HOUR==boundary and p['day_ms']==day
            f['buy_confirmed']=contiguous and p['buy_hypothesis'] and f['buy_hypothesis']
        else:
            f['buy_confirmed']=False
        f['sell_weakness']=False
        if len(hourly)>=2:
            a,b=hourly[-2:]
            same=a['day_ms']==b['day_ms']==day and a['observed_ms']+HOUR==b['observed_ms'] and b['observed_ms']+HOUR==boundary
            down1=sum(b['values'][k]-a['values'][k]<-EPS for k in KEYS)
            down2=sum(f['values'][k]-b['values'][k]<-EPS for k in KEYS)
            f['sell_weakness']=same and down1>=2 and down2>=2 and f['hour_close']<b['hour_low']
        hourly.append(f)
    return days,hourly


def probe(hourly,minutes,activation):
    trades=[];position=None;pending=None
    for p in hourly:
        t=p['observed_ms']
        if position and t<=position['entry_ms']:
            continue
        if position:
            held=[r for r in minutes if position['entry_ms']<=r[0] and r[0]+STEP<=t]
            position['peak']=max(position['peak'],max(r[2] for r in held))
            position['active']=position['active'] or position['peak']>=position['entry_reference']*(1+activation/100)
            action='SELL' if position['active'] and p['sell_weakness'] else None
        else:
            action='BUY' if p['buy_confirmed'] else None
        if action:
            fill=next((r for r in minutes if r[0]>=t),None)
            event=dict(side=action,signal_ms=t,signal_kst=clock(t),daily_label=p['day'],
                       fill_ms=fill[0] if fill else None,fill_kst=clock(fill[0]) if fill else None,
                       reference=fill[1] if fill else None)
            if fill is None:
                pending=event;break
            trades.append(event)
            if action=='BUY':
                position=dict(entry_ms=fill[0],entry_reference=fill[1],peak=fill[1],active=False)
            else:
                position=None
    return dict(trades=trades,open_position=position,pending=pending)


def main():
    scope=json.loads((ROOT/'scope.json').read_text())
    cutoff=int(datetime.fromisoformat(scope['data_cutoff_kst']).timestamp()*1000)
    daily=read_gzip(SOURCE/'daily.json.gz');minutes=read_gzip(SOURCE/'minutes5.json.gz')
    assert all(r[0]+DAY_MS<=cutoff for r in daily)
    assert minutes[-1][0]+STEP==cutoff
    days,hourly=build_features(daily,minutes,cutoff)
    run=probe(hourly,minutes,scope['parameters']['activation_gross_pct'])
    for event in run['trades']:
        # Replay every prior hourly feature, then mask every fill-candle field
        # except its open. No trade can depend on prices after its decision.
        prior_points=[p for p in hourly if p['observed_ms']<=event['signal_ms']]
        prior_rows=[r[:] for r in minutes if r[0]<=event['fill_ms']]
        prior_rows[-1][2:5]=[prior_rows[-1][1]]*3;prior_rows[-1][5:]=[0,0]
        repeated=probe(prior_points,prior_rows,scope['parameters']['activation_gross_pct'])
        assert repeated['trades']==[e for e in run['trades'] if e['signal_ms']<=event['signal_ms']]
    # Recompute intraday indicators after removing every later minute and day.
    for label in ('09/11','09/16','09/18','09/22'):
        p=next((p for p in hourly if p['day']==label and p['buy_confirmed']),None)
        if p:
            t=p['observed_ms']
            past_daily=[r for r in daily if r[0]+DAY_MS<=t]
            _,short=build_features(past_daily,[r for r in minutes if r[0]+STEP<=t],t)
            assert short[-1]==p,'INTRADAY_FEATURE_USED_FUTURE_DATA'
    first_buy={}
    for p in hourly:
        if p['buy_confirmed'] and p['day'] not in first_buy:
            first_buy[p['day']]=dict(observed_kst=p['observed_kst'],price=p['hour_close'],
                values=p['values'],velocity=p['velocity'],acceleration=p['acceleration'])
    # Current incomplete day is described only, never labeled as Sep26 daily.
    live_day=(cutoff-1)//DAY_MS*DAY_MS
    current_rows=[r for r in minutes if r[0]>=live_day]
    completed=[Candle(r[0],r[2],r[3],r[4],r[5]) for r in daily if r[0]<live_day]
    live=Candle(live_day,max(r[2] for r in current_rows),min(r[3] for r in current_rows),current_rows[-1][4],sum(r[5] for r in current_rows))
    live_values=indicators([*completed,live])
    target_ranges=[]
    for day in ('09/11','09/12','09/14','09/18','09/20','09/22','09/25'):
        subset=[r for r in minutes if clock(r[0]//DAY_MS*DAY_MS)[:5]==day]
        hi=max(r[2] for r in subset)
        target_ranges.append(dict(day=day,open=subset[0][1],high=hi,low=min(r[3] for r in subset),last=subset[-1][4],
            first_high_bucket_kst=clock(next(r[0] for r in subset if r[2]==hi)),last_observed_kst=clock(subset[-1][0]+STEP)))
    result=dict(scope=scope,daily=days,
                hourly_diagnostic_points=[p for p in hourly if p['buy_confirmed'] or p['sell_weakness']],
                hourly_point_count=len(hourly),first_buy_confirmation_by_day=first_buy,
                candidate_probe=run,target_ranges=target_ranges,
                current_provisional_day=dict(day=clock(live_day)[:5],observed_kst=clock(cutoff),values=live_values,price=live.close),
                checks=['COMPLETED_DAY_ONLY_BASELINE','PAST_ONLY_INTRADAY_FEATURE_RECOMPUTATION',
                        'FUTURE_FILL_FIELDS_EXCLUDED','TARGET_LABELS_NOT_IN_SIGNAL_PREDICATES'])
    (ROOT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(daily_buy_dates=[d['day'] for d in days if d['buy_hypothesis']],
                         first_buy_confirmations=first_buy,probe=run,current=result['current_provisional_day']),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
