"""Read-only, retrospective time-shift analysis of the selected WAVE catalog.

This is NOT an unconditional noise model: formation's global episode/cooldown
censors venue triggers. Time-block matching does not remove common market news.
No causal confidence, false-alarm probability, or trading probability is emitted.
Existing collection, state transitions, evaluation cohorts and storage stay intact.
"""
import asyncio
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import json
from math import isfinite
from pathlib import Path
import sqlite3
from statistics import mean, median
import time

SCHEMA = 'wave-timeshift-catalog-v1'
WINDOW_MS = 86400000
RESPONSE_MS = 30000
MATURITY_MS = 310000  # NORMALIZATION captures first venue hits after 300 seconds.
SHIFTS_MS = tuple(-m * 60000 for m in (11, 17, 29, 43, 59))
BLOCK_MS = 4 * 3600000
MIN_CONTROLS = 2
VENUES = {'binance', 'bybit', 'kraken', 'upbit', 'bithumb', 'coinone'}
DIMENSIONS = ('asset', 'direction', 'horizon', 'origin_venue', 'follower_venue')
LIMITATIONS = [
    'Selected episode catalog; first-hit and cooldown censoring affect shifted controls.',
    'Matched only within a four-hour time block, not volatility/liquidity/news regime.',
    'Overlapping market shocks and reused controls are not independent samples.',
    'No multiple-testing-adjusted significance, causal confidence or calibrated prediction.',
    'Quote coverage is required; missing observation is never counted as no reaction.',
]


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


class Intervals:
    """Exact union of observed quote intervals; never bridges an unobserved gap."""
    def __init__(self, spans):
        self.spans = []
        for lo, hi in sorted(spans):
            if hi < lo:
                continue
            if self.spans and lo <= self.spans[-1][1]:
                self.spans[-1][1] = max(hi, self.spans[-1][1])
            else:
                self.spans.append([lo, hi])
        self.starts = [p[0] for p in self.spans]

    def covers(self, lo, hi):
        i = bisect_right(self.starts, lo) - 1
        return i >= 0 and self.spans[i][1] >= hi


def first_hit(times, ts):
    i = bisect_left(times, ts)
    return times[i] - ts if i < len(times) and times[i] <= ts + RESPONSE_MS else None


def read_records(root, now):
    """Separate read-only SQLite snapshot; no ingestion lock or raw-file replay."""
    path = (Path(root) / 'research.db').resolve()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        result = {}
        for kind in ('flow_state', 'reception_trial_v3', 'evaluation', 'diagnostics'):
            # Formation just before the window can contain in-window venue hits.
            start = now - WINDOW_MS - (600000 if kind == 'diagnostics' else MATURITY_MS)
            result[kind] = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM records WHERE kind=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms',
                (kind, start, now))]
        return result
    finally:
        db.close()


def build_report(records, now):
    start = now - WINDOW_MS
    states = records.get('flow_state', [])
    episodes = {}
    for row in states:
        if row.get('event_ts_ms', now) >= now:
            continue
        ident = row.get('shock_id')
        ts = row.get('origin_ts_ms', row.get('event_ts_ms'))
        if not ident or not finite(ts):
            continue
        ep = episodes.setdefault(ident, {
            'shock_id': ident, 'event_ts_ms': ts, 'asset': row.get('asset'),
            'direction': row.get('direction'), 'horizon': row.get('horizon'),
            'origin_venue': row.get('origin_venue'), 'venues': {},
            'feature': row.get('origin_feature', {}), 'state': row.get('state'),
            'complete': False,
        })
        ep['event_ts_ms'] = min(ep['event_ts_ms'], ts)
        ep['state'] = row.get('state')
        ep['complete'] = ep['complete'] or row.get('state') == 'NORMALIZATION'
        for venue, hit in row.get('venues', {}).items():
            stamp = hit.get('ts_ms')
            if venue in VENUES and finite(stamp) and ep['event_ts_ms'] <= stamp < now:
                old = ep['venues'].get(venue)
                if old is None or stamp < old['ts_ms']:
                    ep['venues'][venue] = hit

    # Only finalized episodes enter the catalog; unfinished events are display-only.
    catalog = defaultdict(set)
    for ep in episodes.values():
        if ep['complete'] and ep['event_ts_ms'] + MATURITY_MS <= now:
            for venue, hit in ep['venues'].items():
                catalog[(ep['asset'], ep['direction'], ep['horizon'], venue)].add(hit['ts_ms'])
    catalog = {key: sorted(value) for key, value in catalog.items()}

    trials = {}
    conflicts = set()
    for row in records.get('reception_trial_v3', []):
        if not start <= row.get('origin_ts_ms', -1) < now or row.get('event_ts_ms', now) >= now:
            continue
        key = (row.get('shock_id'),) + tuple(row.get(k) for k in DIMENSIONS)
        if row.get('status') not in ('RECEIVED', 'NON_REACTION', 'UNOBSERVABLE'):
            continue
        if key in trials and (trials[key]['status'], trials[key]['origin_ts_ms']) != (row['status'], row['origin_ts_ms']):
            conflicts.add(key)
        trials[key] = row
    for key in conflicts:
        del trials[key]

    spans = defaultdict(list)
    for row in trials.values():
        if row['status'] != 'UNOBSERVABLE':
            lo, hi = row['origin_ts_ms'], row['event_ts_ms']
            if hi >= lo + RESPONSE_MS:
                spans[(row['asset'], row['follower_venue'])].append((lo, hi))
    coverage = {key: Intervals(value) for key, value in spans.items()}

    diags = sorted((r for r in records.get('diagnostics', []) if r.get('event_ts_ms', now) < now),
                   key=lambda r: r['event_ts_ms'])
    diag_times = [r['event_ts_ms'] for r in diags]

    def timing_floor(ts, origin, follower):
        i = bisect_right(diag_times, ts) - 1
        if i < 0 or ts - diag_times[i] > 600000:
            return None
        pair = diags[i].get('pair_timing_resolution', {}).get(origin + '->' + follower, {})
        value = pair.get('min_resolvable_lead_ms')
        return max(1000, value) if pair.get('measurement_ready') and finite(value) else None

    groups = {}
    for key, row in trials.items():
        dims = key[1:]
        g = groups.setdefault(dims, {'counts': Counter(), 'exclusions': Counter(),
                                    'paired': [], 'lags': [], 'resolved_lags': []})
        g['counts'][row['status']] += 1
        if row['status'] == 'UNOBSERVABLE':
            continue
        ep = episodes.get(row['shock_id'])
        ts = row['origin_ts_ms']
        if not ep or not ep['complete'] or ts + MATURITY_MS > now:
            g['exclusions']['UNFINISHED_EPISODE'] += 1
            continue
        times = catalog.get((row['asset'], row['direction'], row['horizon'], row['follower_venue']), [])
        lag = first_hit(times, ts)
        actual = int(lag is not None)
        if actual != (row['status'] == 'RECEIVED'):
            g['exclusions']['CATALOG_STATUS_MISMATCH'] += 1
            continue
        # Compare the same statistic in actual/shifted windows on a matched cohort.
        valid = coverage.get((row['asset'], row['follower_venue']))
        if not valid or not valid.covers(ts, ts + RESPONSE_MS):
            g['exclusions']['ACTUAL_COVERAGE_GAP'] += 1
            continue
        controls = []
        for shift in SHIFTS_MS:
            shifted = ts + shift
            if shifted < start or shifted // BLOCK_MS != ts // BLOCK_MS:
                continue
            if not valid.covers(shifted, shifted + RESPONSE_MS):
                continue
            controls.append(int(first_hit(times, shifted) is not None))
        if len(controls) < MIN_CONTROLS:
            g['exclusions']['INSUFFICIENT_COVERED_CONTROLS'] += 1
            continue
        g['paired'].append((actual, mean(controls), len(controls)))
        if lag is not None:
            g['lags'].append(lag)
            floor = timing_floor(ts, row['origin_venue'], row['follower_venue'])
            if floor is not None and lag > floor:
                g['resolved_lags'].append(lag)

    edges = []
    for dims, g in sorted(groups.items()):
        n = len(g['paired'])
        actual = mean(x[0] for x in g['paired']) if n else None
        background = mean(x[1] for x in g['paired']) if n else None
        counts = g['counts']
        observed = counts['RECEIVED'] + counts['NON_REACTION']
        edges.append({**dict(zip(DIMENSIONS, dims)), 'received': counts['RECEIVED'],
                      'non_reaction': counts['NON_REACTION'], 'unobservable': counts['UNOBSERVABLE'],
                      'observed_n': observed, 'matched_n': n,
                      'control_windows': sum(x[2] for x in g['paired']),
                      'actual_rate': actual, 'control_rate': background,
                      'lift_pp': (actual - background) * 100 if n else None,
                      'lag_median_ms': median(g['lags']) if g['lags'] else None,
                      'resolved_lag_n': len(g['resolved_lags']),
                      'resolved_lag_median_ms': median(g['resolved_lags']) if g['resolved_lags'] else None,
                      'status': 'EXPLORATORY' if n >= 30 else 'INSUFFICIENT_MATCHED_SAMPLE',
                      'exclusions': dict(g['exclusions']), 'calibrated_probability': None})
    # Rank by evidence quantity, never by best-looking lift; all edges are exported.
    edges.sort(key=lambda e: (-e['matched_n'], -e['observed_n'], tuple(e[k] for k in DIMENSIONS)))

    recent = []
    for ep in sorted(episodes.values(), key=lambda e: (-e['event_ts_ms'], e['shock_id'])):
        if not start <= ep['event_ts_ms'] < now:
            continue
        feature = ep['feature']
        volume = feature.get('volume')
        signed = feature.get('signed_volume')
        pressure = signed / volume if finite(volume) and volume > 0 and finite(signed) and abs(signed) <= volume else None
        followers = []
        for venue, hit in sorted(ep['venues'].items()):
            lag = hit['ts_ms'] - ep['event_ts_ms']
            if venue == ep['origin_venue'] or not 0 <= lag <= RESPONSE_MS:
                continue
            floor = timing_floor(ep['event_ts_ms'], ep['origin_venue'], venue)
            followers.append({'venue': venue, 'lag_ms': lag,
                              'timing_resolved': floor is not None and lag > floor})
        recent.append({k: ep[k] for k in ('shock_id', 'asset', 'direction', 'horizon', 'event_ts_ms', 'origin_venue', 'state')} | {
            'return_bps': feature.get('return_bps'), 'volume_acceleration': feature.get('volume_acceleration'),
            'net_flow_ratio': pressure, 'spread_bps': feature.get('spread_bps'),
            'followers': followers, 'finalized': ep['complete'], 'calibrated_probability': None})
        if len(recent) == 30:
            break

    outcomes = {}
    for row in records.get('evaluation', []):
        if (row.get('evaluation_version') == 'quote-v2' and row.get('cohort') == 'FLOW_ONLY'
                and row.get('status') == 'COMPLETE' and row.get('direction') == 'BUY'
                and start <= row.get('event_ts_ms', -1) < now and finite(row.get('forward_return'))):
            outcomes[(row['shock_id'], row['horizon_sec'])] = row
    performance = []
    for horizon in (10, 30, 60, 300):
        xs = [r['forward_return'] for r in outcomes.values() if r['horizon_sec'] == horizon]
        performance.append({'horizon_sec': horizon, 'n': len(xs),
                            'mean_return_pct': mean(xs) * 100 if xs else None,
                            'positive_rate': sum(r > 0 for r in xs) / len(xs) if xs else None})
    return {'schema_version': SCHEMA, 'generated_ts_ms': now, 'expires_ts_ms': now + 900000,
            'window_start_ms': start, 'window_end_ms': now, 'mode': 'RESEARCH_ONLY',
            'execution_eligible': False, 'calibrated_probability': None,
            'method': {'shifts_minutes': [s // 60000 for s in SHIFTS_MS],
                       'response_seconds': 30, 'min_controls_per_case': MIN_CONTROLS,
                       'matching': 'same_4h_UTC_block_only', 'regime_adjusted': False,
                       'source': 'selected_finalized_formation_catalog', 'multiple_testing_adjusted': False},
            'counts': {'episodes': sum(start <= e['event_ts_ms'] < now for e in episodes.values()),
                       'trials': len(trials), 'conflicting_trials_excluded': len(conflicts),
                       'matched_trials': sum(e['matched_n'] for e in edges), 'edge_groups': len(edges)},
            'recent': recent, 'edges': edges, 'price_response': performance,
            'price_response_basis': 'Upbit BUY, initial formation ask to later bid; completed in window; fees/depth slippage excluded',
            'trade_readiness': 'NOT_VALIDATED', 'limitations': LIMITATIONS}


def publish(root, now):
    report = build_report(read_records(root, now), now)
    path = Path(root) / 'exports' / 'wave_latest.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(report, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    tmp.replace(path)
    return report


async def run(root, log):
    while True:
        try:
            began = time.monotonic()
            report = await asyncio.to_thread(publish, root, time.time_ns() // 1000000)
            log.info('wave_analysis=%s', json.dumps({'version': SCHEMA, **report['counts'],
                     'top_edges': report['edges'][:3], 'duration_ms': round((time.monotonic() - began) * 1000)},
                     separators=(',', ':')))
        except Exception as exc:
            log.warning('wave_analysis_failed type=%s', type(exc).__name__)
        await asyncio.sleep(300)
