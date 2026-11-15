"""Collect only public ARK candles for an explicitly frozen historical cutoff."""
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path

from research.september_exit_validation import collect as public

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'


def ms(value):
    return int(datetime.fromisoformat(value).timestamp()*1000)


def write(name, value):
    (DATA/name).write_bytes(gzip.compress(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),mtime=0))


def main():
    policy=json.loads((ROOT/'policy.json').read_text())
    DATA.mkdir(parents=True,exist_ok=True)
    start=ms(policy['window_start_utc']);end=ms(policy['completed_bar_cutoff_utc'])
    started=datetime.now(timezone.utc).isoformat()
    raw=public.get('candles/days',market=policy['market'],to=public.iso(end),count=200)
    daily=sorted([public.convert(r) for r in raw if public.convert(r)[0]+86400000<=end])
    assert daily and daily[-1][0]+86400000<=end
    # An incomplete current daily candle may be returned by the API; discard it.
    values={};boundary=end;pages=0
    while boundary>start:
        chunk=public.get('candles/minutes/5',market=policy['market'],to=public.iso(boundary),count=200)
        assert chunk,'EMPTY_PAGE'
        rows=[public.convert(r) for r in chunk]
        oldest=min(r[0] for r in rows)
        assert oldest<boundary,'PAGINATION_DID_NOT_ADVANCE'
        for r in rows:
            if start<=r[0] and r[0]+300000<=end:
                if r[0] in values:
                    assert values[r[0]]==r,'OVERLAP_DISAGREEMENT'
                values[r[0]]=r
        boundary=oldest;pages+=1
        assert pages<35,'EXCESSIVE_PAGES'
    minutes=[values[k] for k in sorted(values)]
    assert minutes[-1][0]+300000<=end
    write('daily.json.gz',daily);write('minutes5.json.gz',minutes)
    manifest=dict(source='https://api.upbit.com/v1/',policy=policy,started=started,
        finished=datetime.now(timezone.utc).isoformat(),requests=public.requests_count,
        columns=['open_ms','open','high','low','close','volume','turnover'],
        daily_count=len(daily),minute_count=len(minutes),minute_pages=pages,
        first_minute=public.iso(minutes[0][0]),last_minute_close=public.iso(minutes[-1][0]+300000),
        last_completed_daily=public.iso(daily[-1][0]),
        missing_minute_buckets=(end-start)//300000-len(minutes))
    (DATA/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(manifest,ensure_ascii=False))


if __name__=='__main__':
    main()
