"""Public-data VPD scan at one fixed completed-minute boundary, any time of day.

An API `to` excludes candle *starts*, not the later trades of an open candle.
Reconstruct today's partial daily candle from completed hours/minutes instead
of querying an open daily candle sequentially across hundreds of markets.
"""
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

from magi2.closed_day_scan import KST, SCORER, completed, formulas, score_rows

SCHEMA = 'vpd-point-in-time-v1'
MAX_AGE_SECONDS = 600


def cutoff_for(now):
    if now.tzinfo is None:
        raise ValueError('Timezone required')
    return now.astimezone(KST).replace(second=0, microsecond=0)


def trading_day_start(cutoff):
    start = cutoff.replace(hour=9, minute=0, second=0, microsecond=0)
    return start if cutoff >= start else start - timedelta(days=1)


def analyse(market, daily, hourly, minutes, cutoff):
    start = trading_day_start(cutoff)
    days = completed(daily, start, timedelta(days=1))[-35:]
    if len(days) < 20:
        return None, 'SHORT_HISTORY'
    if days[-1][0] + timedelta(days=1) != start:
        return None, 'NO_PREVIOUS_CLOSED_DAY'
    hour = cutoff.replace(minute=0)
    if cutoff > start:
        bars = [(ts, row) for ts, row in completed(hourly, hour, timedelta(hours=1)) if ts >= start]
        bars += [(ts, row) for ts, row in completed(minutes, cutoff, timedelta(minutes=1)) if ts >= hour]
        bars.sort(key=lambda item: item[0])
        previous = float(days[-1][1]['trade_price'])
        fields = ('trade_price', 'high_price', 'low_price', 'candle_acc_trade_volume', 'candle_acc_trade_price')
        for _, row in bars:
            values = [float(row[name]) for name in fields]
            if not np.isfinite(values).all() or min(values[:3]) <= 0 or min(values[3:]) < 0:
                raise ValueError('Invalid intraday OHLCV')
            if not values[2] <= values[0] <= values[1]:
                raise ValueError('Invalid intraday price range')
        # No-trade intervals have no Upbit candle. Carry the prior close with
        # zero volume; never invent trades or drop the latest real closed day.
        partial = {
            'trade_price': float(bars[-1][1]['trade_price']) if bars else previous,
            'high_price': max(float(row['high_price']) for _, row in bars) if bars else previous,
            'low_price': min(float(row['low_price']) for _, row in bars) if bars else previous,
            'candle_acc_trade_volume': sum(float(row['candle_acc_trade_volume']) for _, row in bars),
            'candle_acc_trade_price': sum(float(row['candle_acc_trade_price']) for _, row in bars),
        }
        days = (days + [(start, partial)])[-35:]
    row, reason = score_rows(market, days, hourly, hour)
    if row:
        row['signal_asof'] = cutoff.isoformat()
        row['daily_basis'] = 'CLOSED_DAY' if cutoff == start else 'PARTIAL_DAY_FROM_CLOSED_BARS'
        row['hour_return_asof'] = hour.isoformat()
    return row, reason


class PublicClient:
    """Four read workers share one <= 6.25 requests/sec limiter and deadline."""
    def __init__(self):
        self.local = threading.local()
        self.lock = threading.Lock()
        self.next_request = 0.
        self.deadline = time.monotonic() + 480
        self.cancelled = threading.Event()

    def get(self, path, params):
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
            self.local.session.headers['User-Agent'] = 'MAGI2-PointInTime/1'
        for attempt in range(3):
            with self.lock:
                delay = max(0., self.next_request - time.monotonic())
                if self.cancelled.is_set() or time.monotonic() + delay >= self.deadline:
                    raise TimeoutError('VPD scan deadline exceeded')
                if self.cancelled.wait(delay):
                    raise TimeoutError('VPD scan cancelled')
                self.next_request = time.monotonic() + .16
            try:
                response = self.local.session.get('https://api.upbit.com/v1' + path, params=params, timeout=8)
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                self.cancelled.wait(attempt + 1)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    with self.lock:
                        self.next_request = max(self.next_request, time.monotonic() + attempt + 1)
                    continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError('Unexpected public API response')
            return data
        raise RuntimeError('Public API retry limit')


def read_snapshot(path, now, expected_asof=None):
    try:
        snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
        if snapshot.get('schema') != SCHEMA or snapshot.get('basis') != 'POINT_IN_TIME':
            return None
        asof = datetime.fromisoformat(snapshot['asof'])
        generated = datetime.fromisoformat(snapshot['generated_at'])
        if asof != cutoff_for(asof) or not asof <= generated <= now:
            return None
        if not 0 <= (now - asof).total_seconds() <= MAX_AGE_SECONDS:
            return None
        if expected_asof is not None and asof.isoformat() != expected_asof:
            return None
        if not snapshot.get('all_rows') or not snapshot.get('top10'):
            return None
        return snapshot
    except (OSError, ValueError, TypeError, KeyError):
        return None


def build_snapshot(path, now=None, client=None, clock=None):
    clock = clock or (lambda: datetime.now(KST))
    now = now or clock()
    cutoff = cutoff_for(now)
    cached = read_snapshot(path, now, cutoff.isoformat())
    if cached:
        return cached
    start, hour = trading_day_start(cutoff), cutoff.replace(minute=0)
    client = client or PublicClient()
    markets = sorted({r['market'] for r in client.get('/market/all', {'is_details': 'false'})
                      if r['market'].startswith('KRW-')})
    if not markets:
        raise ValueError('Empty market universe')
    formulas()  # Load pure scorer once before worker threads start.
    def scan_market(market):
        def fetch(kind, boundary, count):
            return client.get('/candles/' + kind, {'market': market, 'count': count,
                              'to': boundary.astimezone(timezone.utc).isoformat()})
        daily = fetch('days', start, 35)
        hourly = fetch('minutes/60', hour, 26)
        minutes = fetch('minutes/1', cutoff, 60) if cutoff > hour else []
        row, reason = analyse(market, daily, hourly, minutes, cutoff)
        return row, {'market': market, 'reason': reason} if row is None else None
    rows, excluded = [], []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='vpd-market') as pool:
        try:
            for row, reason in pool.map(scan_market, markets):
                (rows if row is not None else excluded).append(row if row is not None else reason)
        except Exception:
            if isinstance(client, PublicClient):
                client.cancelled.set()
            raise
    rows.sort(key=lambda row: (-row['VPD'], -row['TodayValue/10'], row['coin']))
    if len(rows) < 10:
        raise ValueError('Insufficient complete VPD candidates')
    for rank, row in enumerate(rows, 1):
        row['Rank'] = rank
    generated = clock()
    if not cutoff <= generated or (generated - cutoff).total_seconds() > MAX_AGE_SECONDS:
        raise TimeoutError('Scan is already stale; no publication')
    snapshot = {
        'schema': SCHEMA, 'basis': 'POINT_IN_TIME', 'scanner': 'VPD v1.5 base score / point in time',
        'asof': cutoff.isoformat(), 'asof_kst': cutoff.strftime('%Y-%m-%d %H:%M:%S KST'),
        'requested_at': now.isoformat(), 'generated_at': generated.isoformat(),
        'trading_day_start': start.isoformat(), 'market_count': len(markets),
        'analysed_count': len(rows), 'excluded': excluded,
        'formula_source_sha256': hashlib.sha256(SCORER.read_bytes()).hexdigest(),
        'top10': rows[:10], 'all_rows': {row['coin']: row for row in rows},
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(destination)
    return snapshot
