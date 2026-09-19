"""Read-only, on-demand VPD scan ending at today's 09:00 KST.

Runs outside the PAPER order worker. Never imports the scanner's top-level
publishing code, changes MAGI1 data, or submits orders.
"""
import ast
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

KST = ZoneInfo('Asia/Seoul')
SCHEMA = 'vpd-closed-day-v1'
SCORER = Path(__file__).resolve().parents[1] / 'scanner/vpd_scanner_v1_4.py'


def cutoff_for(now):
    return now.astimezone(KST).replace(hour=9, minute=0, second=0, microsecond=0)


@lru_cache(maxsize=1)
def formulas():
    """Reuse exactly the baseline's five pure functions, without its I/O."""
    source = SCORER.read_text(encoding='utf-8')
    names = {'calc_rsi', 'calc_williams', 'safe_ratio', 'score_vpd', 'calc_momentum'}
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != names:
        raise RuntimeError('VPD formula contract changed')
    env = {'pd': pd, 'np': np}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SCORER), 'exec'), env)
    return env


def completed(candles, cutoff, duration):
    rows = []
    for candle in candles:
        start = datetime.fromisoformat(candle['candle_date_time_utc']).replace(tzinfo=timezone.utc)
        if start + duration <= cutoff:
            rows.append((start, candle))
    rows.sort(key=lambda x: x[0])
    if len({start for start, _ in rows}) != len(rows):
        raise ValueError('Duplicate candle')
    return rows


def analyse(market, daily, hourly, cutoff):
    days = completed(daily, cutoff, timedelta(days=1))[-35:]
    if len(days) < 20:
        return None, 'SHORT_HISTORY'
    if days[-1][0] + timedelta(days=1) != cutoff:
        return None, 'NO_CLOSED_DAY'
    return score_rows(market, days, hourly, cutoff)


def score_rows(market, days, hourly, cutoff):
    """Score already time-validated daily rows and completed hourly candles."""
    if len(days) < 20:
        return None, 'SHORT_HISTORY'
    df = pd.DataFrame([c for _, c in days])
    for name in ('trade_price', 'high_price', 'low_price', 'candle_acc_trade_volume', 'candle_acc_trade_price'):
        df[name] = pd.to_numeric(df[name], errors='raise')
        if not np.isfinite(df[name]).all() or (df[name] < 0).any():
            raise ValueError('Invalid OHLCV')
    if (df.trade_price <= 0).any():
        raise ValueError('Nonpositive candle price')
    f = formulas()
    rsi, will = f['calc_rsi'](df.trade_price), f['calc_williams'](df)
    volume, value = df.candle_acc_trade_volume, df.candle_acc_trade_price
    v3 = f['safe_ratio'](volume.iloc[-3:].mean(), volume.iloc[-11:-1].mean())
    today = f['safe_ratio'](value.iloc[-1], value.iloc[-11:-1].mean())
    accel = f['safe_ratio'](value.iloc[-1], value.iloc[-4:-1].mean())
    if not np.isfinite([v3, today, accel]).all():
        return None, 'NO_VOLUME_BASE'
    prior = value.iloc[-11:-1]
    spike = bool(prior.median() > 0 and prior.max() > 0 and
                 prior.max()/prior.median() >= 5 and value.iloc[-1]/prior.max() <= .25)
    px, prev = float(df.trade_price.iloc[-1]), float(df.trade_price.iloc[-2])
    high = max(px, float(df.high_price.iloc[-1]))
    day_return, max_return = (px/prev-1)*100, (high/prev-1)*100
    hours = completed(hourly, cutoff, timedelta(hours=1))[-2:]
    hour_return = np.nan
    if (len(hours) == 2 and hours[-1][0]+timedelta(hours=1) == cutoff and
            hours[-2][0]+timedelta(hours=1) == hours[-1][0]):
        before, after = float(hours[-2][1]['trade_price']), float(hours[-1][1]['trade_price'])
        if not np.isfinite([before, after]).all() or min(before, after) <= 0:
            raise ValueError('Invalid hourly price')
        hour_return = (after/before-1)*100
    score = f['score_vpd'](v3,today,accel,rsi.iloc[-1],will.iloc[-1],max_return,spike)
    momentum = f['calc_momentum'](rsi.iloc[-1],rsi.iloc[-2],will.iloc[-1],will.iloc[-2],hour_return,v3)
    row = dict(coin=market.removeprefix('KRW-'), market=market, price=px, VPD=score,
               momentum=momentum, SpikeCollapse=spike, DayHigh=high,
               **{'1D%':day_return, 'MaxPriceReturn%':max_return,
                  'Giveback%p':max_return-day_return, '1H%':hour_return,
                  'RSI14':rsi.iloc[-1], 'Williams':will.iloc[-1],
                  'Vol3/10':v3, 'TodayValue/10':today, 'IntraAccel':accel})
    row = json.loads(pd.DataFrame([row]).to_json(orient='records',double_precision=8))[0]
    return row, None


class PublicClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'MAGI2-ClosedDay/1'
        self.deadline = time.monotonic() + 240
        self.next_request = 0.

    def get(self, path, params):
        for attempt in range(3):
            if time.monotonic() >= self.deadline:
                raise TimeoutError('Closed-day scan exceeded its deadline')
            time.sleep(max(0., self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+.25
            try:
                response = self.session.get('https://api.upbit.com/v1'+path, params=params, timeout=6)
            except (requests.Timeout, requests.ConnectionError):
                if attempt==2: raise
                time.sleep(attempt+1)
                continue
            if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                time.sleep(attempt+1)
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError('Unexpected public API response')
            return data
        raise RuntimeError('Public API rate limited')


def read_snapshot(path, now):
    try:
        snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
        cutoff = cutoff_for(now)
        if now < cutoff or snapshot.get('schema') != SCHEMA:
            return None
        if snapshot.get('asof') != cutoff.isoformat() or not snapshot.get('all_rows') or not snapshot.get('top10'):
            return None
        generated = datetime.fromisoformat(snapshot['generated_at'])
        if not cutoff <= generated <= now:
            return None
        return snapshot
    except (OSError, ValueError, TypeError, KeyError):
        return None


def build_snapshot(path, now=None, client=None, clock=None):
    clock = clock or (lambda: datetime.now(KST))
    now = now or clock()
    cutoff = cutoff_for(now)
    if now < cutoff:
        raise ValueError('The daily candle is not closed yet')
    client = client or PublicClient()
    markets = sorted({r['market'] for r in client.get('/market/all', {'is_details':'false'})
                      if r['market'].startswith('KRW-')})
    if not markets:
        raise ValueError('Empty market universe')
    end = cutoff.astimezone(timezone.utc).isoformat()
    rows, excluded = [], []
    for market in markets:
        # Query by boundary AND validate returned candle times. Do not assume
        # that index -2 is yesterday: an untraded new candle may be absent.
        daily = client.get('/candles/days', {'market':market,'count':36,'to':end})
        hourly = client.get('/candles/minutes/60', {'market':market,'count':3,'to':end})
        row, reason = analyse(market, daily, hourly, cutoff)
        if row is None:
            excluded.append({'market':market,'reason':reason})
        else:
            rows.append(row)
    rows.sort(key=lambda r: (-r['VPD'], -r['TodayValue/10'], r['coin']))
    if len(rows) < 10:
        raise ValueError('Insufficient complete VPD candidates')
    for rank, row in enumerate(rows, 1):
        row['Rank'] = rank
    snapshot = {'schema':SCHEMA,'scanner':'VPD v1.5 base score / completed day',
                'basis':'CLOSED_DAY','asof':cutoff.isoformat(),
                'asof_kst':cutoff.strftime('%Y-%m-%d %H:%M:%S KST'),
                'generated_at':clock().isoformat(), 'market_count':len(markets),
                'analysed_count':len(rows),'excluded':excluded,
                'formula_source_sha256':hashlib.sha256(SCORER.read_bytes()).hexdigest(),
                'top10':rows[:10],'all_rows':{r['coin']:r for r in rows}}
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix('.tmp')
    temp.write_text(json.dumps(snapshot,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    temp.replace(destination)
    return snapshot
