"""Read public Upbit data; keep selection and exclusions reproducible. No orders."""
from datetime import datetime, timezone, timedelta
import gzip
import json
from pathlib import Path
from statistics import median
import time
import urllib.request
import urllib.parse
import urllib.error

from magi2.fast_wave_indicator import Candle, DAY_MS, indicators

ROOT = Path(__file__).resolve().parent
OUT = ROOT/'data'
POLICY = json.loads((ROOT/'policy.json').read_text())
last_request = 0.
requests_count = 0


def get(path, **params):
    global last_request, requests_count
    url = 'https://api.upbit.com/v1/'+path
    if params:
        url += '?'+urllib.parse.urlencode(params)
    for attempt in range(4):
        time.sleep(max(0, .22-(time.monotonic()-last_request)))
        last_request = time.monotonic()
        requests_count += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'Accept':'application/json'}),timeout=20) as r:
                value = json.load(r)
            return value
        except urllib.error.HTTPError as e:
            if e.code == 418:
                raise  # Respect a blocked response; never evade by changing IP.
            if e.code not in (429,500,502,503,504) or attempt == 3:
                raise
            time.sleep(2**attempt)
        except (TimeoutError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def convert(r):
    ts=int(datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000)
    return [ts]+[r[k] for k in ('opening_price','high_price','low_price','trade_price',
                                'candle_acc_trade_volume','candle_acc_trade_price')]


def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat().replace('+00:00','Z')


def write_gzip(name, value):
    encoded=json.dumps(value,separators=(',',':'),ensure_ascii=False).encode()
    (OUT/name).write_bytes(gzip.compress(encoded,mtime=0))


def signals(market, rows):
    min_history=POLICY['minimum_history']; cfg=POLICY['selection']; e=cfg['epsilon']
    result=[]
    if len(rows)<min_history:
        return result
    bars=[Candle(r[0],r[2],r[3],r[4],r[5]) for r in rows]
    features={}
    for i,row in enumerate(rows):
        date=iso(row[0])[:10]
        if not POLICY['signal_start'] <= date <= POLICY['signal_end'] or i+1<min_history:
            continue
        if any(b.open_ms-a.open_ms!=DAY_MS for a,b in zip(bars[i-149:i],bars[i-148:i+1])):
            continue
        turnover=median(r[6] for r in rows[i-20:i])
        base=median(r[5] for r in rows[i-21:i-1])
        if turnover<cfg['prior20_median_daily_turnover_min_krw'] or base<=0:
            continue
        ratio=rows[i-1][5]/base
        if ratio<cfg['previous_volume_over_prior20_median_min'] or row[5]>=rows[i-1][5]:
            continue
        for j in (i-2,i-1,i):
            if j not in features:
                features[j]=indicators(bars[:j+1])
        a,b,c=(features[j] for j in (i-2,i-1,i))
        velocity={k:c[k]-b[k] for k in c}
        acceleration={k:c[k]-2*b[k]+a[k] for k in c}
        if not (acceleration['macd_hist']>e and all(velocity[k]>e and acceleration[k]>e for k in ('rsi','williams'))):
            continue
        result.append(dict(market=market,signal_day=date,entry_ms=row[0]+DAY_MS,
                           median_turnover_krw=turnover,prior_volume_ratio=ratio,
                           volume_change_pct=(row[5]/rows[i-1][5]-1)*100,
                           values=c,velocity=velocity,acceleration=acceleration))
    return result


def minute_window(market, start, end):
    # Include endpoint bar for its OPEN only; future high/low/close is not input.
    boundary=end+POLICY['bar_minutes']*60000
    values={}; pages=0
    while True:
        chunk=get('candles/minutes/'+str(POLICY['bar_minutes']),market=market,to=iso(boundary),count=200)
        pages+=1
        if not chunk:
            break
        converted=[convert(r) for r in chunk]
        oldest=min(r[0] for r in converted)
        if oldest>=boundary:
            raise ValueError('PAGINATION_DID_NOT_ADVANCE')
        for r in converted:
            if start<=r[0]<=end:
                if r[0] in values and values[r[0]]!=r:
                    raise ValueError('DUPLICATE_BAR_DISAGREEMENT')
                values[r[0]]=r
        if oldest<=start:
            break
        boundary=oldest
        if pages>30:
            raise ValueError('EXCESSIVE_PAGES')
    return [values[k] for k in sorted(values)]


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    started=datetime.now(timezone.utc).isoformat()
    markets=get('market/all',is_details='false')
    krw=sorted([r for r in markets if r['market'].startswith('KRW-')],key=lambda r:r['market'])
    excluded=set(POLICY['excluded_assets'])
    all_daily={}; candidates=[]; failures=[]
    for j,market in enumerate(krw):
        symbol=market['market']
        if symbol.split('-',1)[1] in excluded and symbol!='KRW-ARK':
            continue
        try:
            rows=sorted([convert(r) for r in get('candles/days',market=symbol,
                  to=POLICY['daily_to_exclusive'],count=POLICY['history_count'])],key=lambda r:r[0])
            if len({r[0] for r in rows})!=len(rows):
                raise ValueError('DUPLICATE_DAILY_BAR')
            for r in rows:
                Candle(r[0],r[2],r[3],r[4],r[5]).validate()
                assert r[3]<=r[1]<=r[2]
            all_daily[symbol]=rows
            if symbol!='KRW-ARK':
                candidates.extend(signals(symbol,rows))
        except Exception as exc:
            if isinstance(exc,urllib.error.HTTPError) and exc.code==418:
                raise
            failures.append(dict(market=symbol,error=type(exc).__name__+':'+str(exc)[:120]))
        if j%30==0:
            print('DAILY_PROGRESS',j,len(krw),'events',len(candidates),flush=True)
    candidates.sort(key=lambda r:(r['entry_ms'],-r['median_turnover_krw'],r['market']))
    selected=[]; seen=set()
    for event in candidates:
        if event['market'] in seen:
            continue
        selected.append(event); seen.add(event['market'])
        if len(selected)>=POLICY['sample_size']:
            break
    manifest=dict(started=started,finished=None,policy=POLICY,
                  source='https://api.upbit.com/v1/',columns=['open_ms','open','high','low','close','volume','turnover'],
                  current_krw_markets=krw,daily_markets_collected=len(all_daily),failures=failures,
                  candidate_events=candidates,selected=selected,minute_quality=[])
    write_gzip('daily_all.json.gz',all_daily)
    (OUT/'selection.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print('SELECTION='+json.dumps(selected,ensure_ascii=False),flush=True)
    if len(selected)!=POLICY['sample_size']:
        raise ValueError('INSUFFICIENT_CASES_WITH_FROZEN_RULES:'+str(len(selected)))
    ark=int(datetime.fromisoformat(POLICY['ark_reference_entry']).replace(tzinfo=timezone.utc).timestamp()*1000)
    cases=[dict(market='KRW-ARK',entry_ms=ark,reference_only=True)]+selected
    for event in cases:
        symbol=event['market']; entry=event['entry_ms']; end=entry+POLICY['evaluation_days']*DAY_MS
        start=entry-POLICY['warmup_hours']*3600000
        rows=minute_window(symbol,start,end)
        gaps=[(a[0],b[0]) for a,b in zip(rows,rows[1:]) if b[0]-a[0]!=POLICY['bar_minutes']*60000]
        quality=dict(market=symbol,entry_ms=entry,end_ms=end,count=len(rows),
                     expected=(end-start)//(POLICY['bar_minutes']*60000)+1,
                     gaps=gaps,entry_present=any(r[0]==entry for r in rows),
                     endpoint_present=any(r[0]==end for r in rows))
        manifest['minute_quality'].append(quality)
        write_gzip(symbol+'_5m.json.gz',rows)
        print('MINUTE_QUALITY='+json.dumps(quality),flush=True)
    manifest.update(finished=datetime.now(timezone.utc).isoformat(),request_count=requests_count)
    (OUT/'selection.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print('DONE',len(cases),'cases',requests_count,'requests',flush=True)


if __name__=='__main__':
    main()
