"""Explicit-universe SHADOW probe, for replacement validation; never sends orders.

Run with --help. Each venue has its own process/session/store connection. Selection
of a market-wide top five and execution integration remain rollout work.
"""
import argparse
import json
import time
from magi2.fast_wave_indicator import VENUES, evaluate
from magi2.fast_wave_market import IndicatorMarket
from magi2.fast_wave_store import EvidenceStore


def observe_one(market, store, symbol):
    point = market.observe(symbol)
    store.append(point)
    points = store.recent(market.venue, symbol, point['observed_ms'])
    result = evaluate(points, market.clock())
    if result['technical_candidate']:
        try:
            flow = market.flow(symbol)
        except Exception as exc:
            flow = {'sufficient': False, 'error': type(exc).__name__ + ':' + str(exc)[:100]}
        result = evaluate(points, market.clock(), flow=flow)
    store.record(point, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venue', choices=VENUES, required=True)
    parser.add_argument('--symbols', nargs='+', required=True)
    parser.add_argument('--cohort', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--interval', type=float, default=60.)
    args = parser.parse_args(argv)
    if not 1 <= len(set(args.symbols)) <= 5 or not 1 <= args.samples <= 1440 or not 30 <= args.interval <= 90:
        parser.error('Use 1..5 unique symbols, 1..1440 samples and interval 30..90 seconds')
    store = EvidenceStore(args.root, args.cohort)
    market = IndicatorMarket(args.venue)
    try:
        market.discover()
        if any(symbol not in market.symbols for symbol in args.symbols):
            parser.error('Symbol outside discovered spot universe')
        for index in range(args.samples):
            began = time.monotonic()
            for symbol in dict.fromkeys(args.symbols):
                try:
                    result = observe_one(market, store, symbol)
                    output = {k: result[k] for k in ('ready', 'technical_candidate', 'paper_candidate', 'score', 'reason')}
                except Exception as exc:
                    output = {'ready': False, 'reason': str(exc)[:150]}
                    store.record_error(args.venue, symbol, market.clock(), output['reason'])
                print(json.dumps(dict(output, venue=args.venue, symbol=symbol, mode='SHADOW'), ensure_ascii=False), flush=True)
                # Conservative per-venue pacing. Slow loops are rejected by the evaluator.
                time.sleep(1.1 if args.venue == 'kraken' else .2)
            if index + 1 < args.samples:
                time.sleep(max(0, args.interval - (time.monotonic() - began)))
    finally:
        market.http.close()
        store.close()


if __name__ == '__main__':
    main()
