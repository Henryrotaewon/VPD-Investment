"""Explicit opt-in historical snapshot experiment, separate from the PIT engine."""
import argparse
import json
from pathlib import Path
from .regimes import date_ms, read_coinmetrics, run_regimes


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--btc-csv',required=True); p.add_argument('--eth-csv',required=True)
    p.add_argument('--asof',required=True,help='UTC YYYY-MM-DD')
    p.add_argument('--output',required=True)
    p.add_argument('--allow-historical-snapshot',action='store_true')
    p.add_argument('--macro-report',help='Optional issued forecasts from the existing PIT engine')
    args=p.parse_args()
    if not args.allow_historical_snapshot: p.error('Explicit historical-snapshot opt-in required')
    asof=date_ms(args.asof)
    panel,sources=read_coinmetrics(args.btc_csv,args.eth_csv,date_ms('2017-01-01'),asof)
    macro=json.loads(Path(args.macro_report).read_text()) if args.macro_report else None
    result=run_regimes(panel,asof,evaluation_start=date_ms('2020-01-01'),
                       holdout_start=date_ms('2023-01-01'),macro_report=macro)
    result['sources']=sources
    result['protocol']=json.loads(Path(__file__).with_name('regime_protocol.json').read_text())
    target=Path(args.output); target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('status','current_status','direction_basis','data_end_ms','live_enabled')}))


if __name__=='__main__': main()
