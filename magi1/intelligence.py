"""Atomic, read-only observation handoff; never an order StrategySignal."""
import hashlib
import json
from pathlib import Path

SCHEMA = 'magi1-intelligence-v1'


def observation(row):
    canonical = json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return {
        'observation_id': hashlib.sha256(canonical.encode()).hexdigest(),
        'event_ts_ms': row['event_ts_ms'],
        'asset': row['asset'],
        'strategy_tag': row['signal_type'],
        'direction': row['direction'],
        'heuristic_score': row['score'],
        'calibrated_probability': None,
        'evidence': row['evidence'],
        'source_version': row.get('version', 'derived-v1'),
        'execution_eligible': False,
    }


def publish_intelligence(storage, end_ms, window_ms=300000):
    rows = storage.query('derived_signal', end_ms-window_ms, end_ms)
    observations = {x['observation_id']: x for x in map(observation, rows)}
    payload = {
        'schema_version': SCHEMA, 'producer': 'MAGI1',
        'generated_ts_ms': end_ms, 'window_start_ms': end_ms-window_ms,
        'expires_ts_ms': end_ms+120000,
        'mode': 'OBSERVATION_ONLY',
        'observations': list(observations.values()),
    }
    path = Path(storage.root)/'exports'/'intelligence_latest.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    tmp.replace(path)
    return path
