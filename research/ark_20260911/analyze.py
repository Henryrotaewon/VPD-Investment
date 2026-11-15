"""Reproduce the ARK case from archived public candles; no network or orders."""
from pathlib import Path
from datetime import datetime, timezone
import json
import math
from statistics import median
from magi2.fast_wave_indicator import Candle, DAY_MS, ema, indicators
from magi2.daily_indicator_review import review, COMPONENTS

ROOT=Path(__file__).resolve().parent
raw=json.loads((ROOT/'public_candles.json').read_text())
def candle(row):
    stamp=int(datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc).timestamp()*1000)
    return Candle(stamp,row[2],row[3],row[4],row[5])
rows=[candle(x) for x in raw['training']]
cutoff=int(datetime(2026,9,12,tzinfo=timezone.utc).timestamp()*1000)
assert len(rows)==400 and rows[-1].open_ms+DAY_MS==cutoff
assert all(b.open_ms-a.open_ms==DAY_MS for a,b in zip(rows,rows[1:]))
for c in rows:c.validate()

closes=[c.close for c in rows]
fast,slow=ema(closes,12),ema(closes,26)
macd=[None if a is None or b is None else a-b for a,b in zip(fast,slow)]
signals=[None]*25+ema(macd[25:],9)
features={}
for i in range(33,len(rows)):
    v=indicators(rows[:i+1])
    v.update(macd=macd[i],macd_signal=signals[i],volume=rows[i].volume)
    v['volume_ratio_prior20_mean']=v['volume']/(sum(r.volume for r in rows[i-20:i])/20)
    v['volume_ratio_prior20_median']=v['volume']/median(r.volume for r in rows[i-20:i])
    for n in (5,10,20):v[f'volume_ma{n}']=sum(r.volume for r in rows[i+1-n:i+1])/n
    features[i]=v
rsi=[features[i]['rsi'] for i in range(33,len(rows))]
rsi_ema=ema(rsi,9)
for i in range(41,len(rows)):
    features[i]['rsi_signal_sma9']=sum(rsi[i-33-8:i-33+1])/9
    features[i]['rsi_signal_ema9']=rsi_ema[i-33]

dates=[]
for i in range(len(rows)-15,len(rows)):
    v=features[i];a=features[i-2];b=features[i-1]
    changes={k:dict(velocity=v[k]-b[k],acceleration=v[k]-2*b[k]+a[k]) for k in v}
    data=raw['training'][i]
    dates.append(dict(date=data[0][:10],open=data[1],high=data[2],low=data[3],close=data[4],
                      return_pct=(data[4]/rows[i-1].close-1)*100,
                      volume_change_pct=(data[5]/rows[i-1].volume-1)*100,
                      values=v,changes=changes))

result=review(rows,cutoff,venue='upbit',symbol='KRW-ARK')
# Forming/future prices and volumes must never influence the D-close decision.
assert review(rows+[candle(x) for x in raw['outcomes']],cutoff,venue='upbit',symbol='KRW-ARK')==result
for k in COMPONENTS:
    assert math.isclose(result['changes'][k]['velocity'],dates[-1]['changes'][k]['velocity'],abs_tol=1e-9)
    assert math.isclose(result['changes'][k]['acceleration'],dates[-1]['changes'][k]['acceleration'],abs_tol=1e-8)

# Sensitivity example, not a calibrated rule: equal weights and past-only median
# absolute first differences from 60 days ending September 10. No threshold.
scales={k:median(abs(features[i][k]-features[i-1][k]) for i in range(len(rows)-61,len(rows)-1))
        for k in COMPONENTS}
scaled=review(rows,cutoff,venue='upbit',symbol='KRW-ARK',weights={k:1 for k in COMPONENTS},scales=scales)
contributions={k:{part:result['changes'][k][part]/scales[k] for part in ('velocity','acceleration')}
               for k in COMPONENTS}

first=raw['outcomes'][0]
outcome=dict(date=first[0][:10],opening_price=first[1],close=first[4],
             opening_gap_pct=(first[1]/rows[-1].close-1)*100,
             open_to_close_gross_pct=(first[4]/first[1]-1)*100,
             note='OUTCOME_ONLY_NOT_SIGNAL_INPUT_NOT_ASSUMED_EXECUTION')
summary=dict(market='KRW-ARK',training_days=len(rows),cutoff_utc=raw['training_cutoff_exclusive_utc'],
             dates=dates,review=result,experimental_equal_weight=dict(scales=scales,
             contributions=contributions,composite=scaled['composite'],approved=False),
             next_day_outcome=outcome,checks=dict(no_future_leak=True,contiguous_history=True))
(ROOT/'analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(dates=[x for x in dates if x['date']>='2026-09-09'],
                     experimental_equal_weight=summary['experimental_equal_weight'],outcome=outcome),indent=2))
