"""Small private log summaries; full observations remain in research storage."""
import json
from collections import Counter, defaultdict


def build_summary(storage, now):
    start = now - 86400000
    onchain = Counter()
    edges = defaultdict(Counter)
    with storage._lock:
        for kind, payload in storage.db.execute(
            'SELECT kind,payload FROM records WHERE kind IN (?,?) AND ts_ms>=? AND ts_ms<?',
            ('onchain_candidate', 'propagation', start, now)):
            row = json.loads(payload)
            if kind == 'onchain_candidate':
                onchain['records'] += 1
                tx = row.get('metadata', {}).get('transaction')
                if not tx:
                    onchain['missing_transaction_structure'] += 1
                    continue
                onchain['with_transaction_structure'] += 1
                for side in ('inputs', 'outputs'):
                    for item in tx.get(side, []):
                        onchain[side + '_occurrences'] += 1
                        onchain[side + '_with_address'] += bool(item.get('address'))
                for side in ('input_labels', 'output_labels'):
                    for label in tx.get(side, []):
                        onchain['label_' + label.get('status', 'UNKNOWN')] += 1
            elif row.get('coverage_version') == 'quote-v2':
                key = tuple(row.get(k) for k in ('asset', 'direction', 'horizon', 'origin_venue', 'follower_venue'))
                edges[key]['received' if row.get('received') else 'non_reaction'] += 1
    result = []
    for key, count in sorted(edges.items()):
        n = count['received'] + count['non_reaction']
        result.append(dict(zip(('asset', 'direction', 'horizon', 'origin', 'follower'), key),
                           **count, observed_n=n, observed_probability=count['received']/n))
    return {'version': 'private-summary-v1', 'generated_at_ms': now,
            'window_start_ms': start, 'window_end_ms': now,
            'onchain': dict(onchain), 'edges': result,
            'scope': 'retained 24h records; counts are observations, not unique wallets or independent shocks',
            'limitations': ['onchain labels do not establish market causality',
                           'quote-v2 excludes unobservable trials; exact edge missing denominators unavailable',
                           'overlapping shocks: predictive confidence intervals not estimated']}


def publish_summary(storage, now, logger):
    report = build_summary(storage, now)
    # Checkpoint is included in existing verified research DB backups.
    storage.checkpoint('analysis_summary_v1', report)
    summary = {k:v for k,v in report.items() if k != 'edges'}
    summary['edge_groups'] = len(report['edges'])
    logger.info('analysis_summary=%s', json.dumps(summary, separators=(',', ':')))
    for edge in report['edges']:
        logger.info('analysis_edge=%s', json.dumps({'generated_at_ms': now, **edge}, separators=(',', ':')))
