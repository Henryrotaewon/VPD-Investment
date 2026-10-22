import argparse
import json
from pathlib import Path
from .model import run


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def main():
    parser=argparse.ArgumentParser(description='Offline, point-in-time BTC/ETH macro-index research')
    parser.add_argument('--features',required=True,help='Audited feature JSONL; no latest-only revisions')
    parser.add_argument('--outcomes',required=True,help='Separate realized price-outcome JSONL')
    parser.add_argument('--manifest',default=str(Path(__file__).with_name('manifest.json')))
    parser.add_argument('--asof-ms',required=True,type=int)
    parser.add_argument('--output',required=True)
    parser.add_argument('--plot',help='Optional PNG; requires matplotlib and observed out-of-sample results')
    args=parser.parse_args()
    specs=json.loads(Path(args.manifest).read_text())['features']
    result=run(rows(args.features),rows(args.outcomes),specs,args.asof_ms)
    target=Path(args.output); target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
    if args.plot:
        from .plot import render
        render(result,args.plot)
    print(json.dumps({'status':result['status'],'forecasts':len(result['forecasts']),
                      'live_enabled':False,'output':str(target)}))


if __name__=='__main__': main()
