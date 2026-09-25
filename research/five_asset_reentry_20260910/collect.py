"""Read public candles for five specified markets at a frozen cutoff."""
from datetime import datetime,timezone
import gzip
import json
from pathlib import Path

from research.september_exit_validation import collect as public

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'


def ms(value):
    return int(datetime.fromisoformat(value).timestamp()*1000)


def write(name,value):
    (DATA/name).write_bytes(gzip.compress(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),mtime=0))


def main():
    p=json.loads((ROOT/'policy.json').read_text());DATA.mkdir(parents=True,exist_ok=True)
    start=ms(p['start_utc']);end=ms(p['cutoff_utc'])
    started=datetime.now(timezone.utc).isoformat()
    market_map={r['market']:r for r in public.get('market/all',is_details='false')}
    records=[]
    for market in p['markets']:
        assert market in market_map,'MARKET_NOT_FOUND'
        raw=public.get('candles/days',market=market,to=public.iso(end),count=200)
        daily=sorted([public.convert(r) for r in raw if public.convert(r)[0]+86400000<=end])
        assert sum(r[0]<start for r in daily)>=p['minimum_completed_history'],'INSUFFICIENT_WARMUP'
        values={};boundary=end;pages=0
        while boundary>start:
            raw=public.get('candles/minutes/5',market=market,to=public.iso(boundary),count=200)
            assert raw,'EMPTY_PAGE'
            chunk=[public.convert(r) for r in raw];oldest=min(r[0] for r in chunk)
            assert oldest<boundary,'PAGINATION_DID_NOT_ADVANCE'
            for r in chunk:
                if start<=r[0] and r[0]+p['bar_ms']<=end:
                    if r[0] in values:
                        assert values[r[0]]==r,'OVERLAP_DISAGREEMENT'
                    values[r[0]]=r
            boundary=oldest;pages+=1
            assert pages<40,'EXCESSIVE_PAGES'
        minutes=[values[t] for t in sorted(values)]
        write(market+'_daily.json.gz',daily);write(market+'_5m.json.gz',minutes)
        record=dict(market=market,name=market_map[market]['korean_name'],daily_count=len(daily),
                    minute_count=len(minutes),pages=pages,
                    first_minute=public.iso(minutes[0][0]),last_minute_close=public.iso(minutes[-1][0]+p['bar_ms']),
                    last_completed_daily=public.iso(daily[-1][0]),
                    missing_buckets=(end-start)//p['bar_ms']-len(minutes))
        records.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
    manifest=dict(policy=p,source='https://api.upbit.com/v1/',started=started,
        finished=datetime.now(timezone.utc).isoformat(),requests=public.requests_count,markets=records,
        columns=['open_ms','open','high','low','close','volume','turnover'])
    (DATA/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print('COMPLETE',len(records),'markets',public.requests_count,'requests',flush=True)


if __name__=='__main__':
    main()
