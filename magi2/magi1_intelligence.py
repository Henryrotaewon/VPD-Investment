"""Validate MAGI1 observations for research joins; does not enable trading.

The caller supplies a transported snapshot path. Separate Railway services do
not implicitly share a volume. No credentials or network transport are assumed.
"""
import json
import math
from pathlib import Path


def load_intelligence(path, now_ms):
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    return validate_intelligence(payload, now_ms)


def validate_intelligence(payload, now_ms):
    if payload.get('schema_version') != 'magi1-intelligence-v1':
        raise ValueError('UNSUPPORTED_INTELLIGENCE_SCHEMA')
    if payload.get('mode') != 'OBSERVATION_ONLY':
        raise ValueError('NOT_OBSERVATION_ONLY')
    generated, expires = payload['generated_ts_ms'], payload['expires_ts_ms']
    if not generated <= now_ms < expires or expires-generated > 120000:
        raise ValueError('STALE_OR_FUTURE_INTELLIGENCE')
    rows = []
    seen = set()
    for row in payload['observations']:
        if row.get('execution_eligible') is not False:
            raise ValueError('OBSERVATION_CANNOT_AUTHORIZE_EXECUTION')
        if row['strategy_tag'] not in ('FAST', 'WHALE'):
            raise ValueError('UNKNOWN_OBSERVATION_TYPE')
        score = row['heuristic_score']
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError('INVALID_HEURISTIC_SCORE')
        if not payload['window_start_ms'] <= row['event_ts_ms'] < generated:
            raise ValueError('OBSERVATION_OUTSIDE_WINDOW')
        if row['observation_id'] not in seen:
            rows.append(row)
            seen.add(row['observation_id'])
    return rows


def fetch_intelligence(url, token, now_ms):
    import requests
    if len(token) < 32: raise ValueError('INTELLIGENCE_TOKEN_MISSING')
    with requests.Session() as session:
        session.trust_env = False  # Private-network traffic must not use an outbound proxy.
        response = session.get(url, headers={'Authorization': 'Bearer '+token}, timeout=5,
                               allow_redirects=False)
        if response.status_code != 200: raise ValueError('INTELLIGENCE_UNAVAILABLE')
        return validate_intelligence(response.json(), now_ms)
