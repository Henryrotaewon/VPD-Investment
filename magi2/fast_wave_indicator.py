"""FAST indicator research: completed-day baseline, timestamped intraday acceleration.

Pure calculations only. Scores are experimental evidence counts, not probabilities.
No order client or MAGI1 mutation. The caller supplies normalized UTC daily candles.
"""
from dataclasses import asdict, dataclass
import math
from statistics import median

VERSION = 'fast-wave-indicator-v1'
DAY_MS = 86_400_000
VENUES = ('upbit', 'bithumb', 'binance', 'kraken')
KEYS = ('macd_hist_bps', 'rsi', 'williams', 'volume_pace')


def day_offset(venue):
    # Bithumb native daily candles open at 00:00 KST (15:00 UTC).
    return 15 * 3_600_000 if venue == 'bithumb' else 0


def day_start(venue, ts_ms):
    offset = day_offset(venue)
    return (ts_ms - offset) // DAY_MS * DAY_MS + offset


@dataclass(frozen=True)
class Candle:
    open_ms: int
    high: float
    low: float
    close: float
    volume: float

    def validate(self, offset_ms=0):
        if (not isinstance(self.open_ms, int) or (self.open_ms - offset_ms) % DAY_MS
                or not all(math.isfinite(x) for x in (self.high, self.low, self.close, self.volume))
                or not 0 < self.low <= self.close <= self.high or self.volume < 0):
            raise ValueError('INVALID_DAILY_CANDLE')


@dataclass(frozen=True)
class Policy:
    min_history: int = 100
    min_elapsed_ms: int = 300_000
    min_gap_ms: int = 30_000
    max_gap_ms: int = 180_000
    max_age_ms: int = 90_000
    delta_min: tuple = (.5, 1., 3., .15)
    velocity_min: tuple = (.02, .02, .05, .002)
    acceleration_min: tuple = (.005, .005, .01, .0005)
    min_rising: int = 3
    min_accelerating: int = 2
    min_score: int = 65


def ema(values, period):
    """SMA seed followed by EMA; None until the seed has enough observations."""
    result = [None] * len(values)
    if len(values) < period:
        return result
    value = sum(values[:period]) / period
    result[period - 1] = value
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        value += alpha * (values[i] - value)
        result[i] = value
    return result


def indicators(rows):
    """MACD(12,26,9), Wilder RSI14 and Williams %R14 at the last candle."""
    if len(rows) < 34:
        raise ValueError('INDICATOR_WARMUP')
    closes = [row.close for row in rows]
    fast, slow = ema(closes, 12), ema(closes, 26)
    macd = [fast[i] - slow[i] for i in range(25, len(rows))]
    signal = ema(macd, 9)[-1]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(x, 0) for x in changes[:14]) / 14
    loss = sum(max(-x, 0) for x in changes[:14]) / 14
    for change in changes[14:]:
        gain = (gain * 13 + max(change, 0)) / 14
        loss = (loss * 13 + max(-change, 0)) / 14
    rsi = 50. if gain == loss == 0 else 100. if loss == 0 else 100 - 100 / (1 + gain / loss)
    high = max(row.high for row in rows[-14:])
    low = min(row.low for row in rows[-14:])
    williams = -50. if high == low else -100 * (high - closes[-1]) / (high - low)
    return {'macd_hist': macd[-1] - signal, 'rsi': rsi, 'williams': williams}


def snapshot(completed, current, observed_ms, *, venue, symbol, policy=Policy()):
    """Reject missing days instead of silently interpreting trading days as calendar days.

    Current daily volume is divided by elapsed fraction and prior 20-day median.
    This is a linear pace estimate, NOT a seasonality-adjusted volume forecast.
    """
    if venue not in VENUES or not symbol:
        raise ValueError('INVALID_MARKET')
    if len(completed) < max(34, policy.min_history):
        raise ValueError('INSUFFICIENT_COMPLETED_HISTORY')
    for row in [*completed, current]:
        row.validate(day_offset(venue))
    if any(b.open_ms - a.open_ms != DAY_MS for a, b in zip(completed, completed[1:])):
        raise ValueError('NONCONTIGUOUS_OR_DUPLICATE_HISTORY')
    if completed[-1].open_ms + DAY_MS != current.open_ms:
        raise ValueError('PREVIOUS_DAY_MISSING_OR_CURRENT_IN_HISTORY')
    elapsed = observed_ms - current.open_ms
    if not policy.min_elapsed_ms <= elapsed < DAY_MS:
        raise ValueError('CURRENT_DAY_NOT_READY')
    prior_volume = median(row.volume for row in completed[-20:])
    before_volume = median(row.volume for row in completed[-21:-1])
    if min(prior_volume, before_volume) <= 0:
        raise ValueError('ZERO_VOLUME_BASELINE')
    baseline = indicators(completed)
    present = indicators([*completed, current])
    # Same frozen denominator on both sides; price changes cannot rescale the delta.
    for point in (baseline, present):
        point['macd_hist_bps'] = point.pop('macd_hist') / completed[-1].close * 10_000
    baseline['volume_pace'] = completed[-1].volume / before_volume
    present['volume_pace'] = current.volume / (elapsed / DAY_MS) / prior_volume
    return dict(strategy_version=VERSION, venue=venue, symbol=symbol,
                day_ms=current.open_ms, baseline_ms=completed[-1].open_ms,
                observed_ms=observed_ms, baseline=baseline, current=present,
                delta={k: present[k] - baseline[k] for k in KEYS},
                capture_price=current.close, current_volume=current.volume,
                volume_basis='LINEAR_ELAPSED_PACE_NOT_SEASONALITY_ADJUSTED')


def derivatives(points):
    """Two local slopes and a centered second derivative, per minute / minute²."""
    a, b, c = points
    dt1 = (b['observed_ms'] - a['observed_ms']) / 60_000
    dt2 = (c['observed_ms'] - b['observed_ms']) / 60_000
    if min(dt1, dt2) <= 0:
        raise ValueError('NONINCREASING_OBSERVATIONS')
    velocity, acceleration = {}, {}
    for key in KEYS:
        first = (b['current'][key] - a['current'][key]) / dt1
        second = (c['current'][key] - b['current'][key]) / dt2
        velocity[key] = second
        acceleration[key] = 2 * (second - first) / (dt1 + dt2)
    return velocity, acceleration


def flow_confirmed(flow, now_ms):
    """Use the existing bounded taker-sample contract, with collection freshness."""
    if not flow:
        return False
    fields = ('observed_ms', 'sample_trades', 'sample_span_ms', 'latest_trade_age_ms',
              'buyer_share_pct', 'spread_bps')
    if not all(isinstance(flow.get(k), (int, float)) and math.isfinite(flow[k]) for k in fields):
        return False
    return bool(flow.get('sufficient') is True
                and 0 <= now_ms - flow['observed_ms'] <= 3000
                and flow['sample_trades'] >= 20 and flow['sample_span_ms'] >= 5000
                and 0 <= flow['latest_trade_age_ms'] + now_ms-flow['observed_ms'] <= 3000
                and 70 <= flow['buyer_share_pct'] <= 100 and 0 <= flow['spread_bps'] <= 10)


def evaluate(points, now_ms, *, flow=None, policy=Policy()):
    result = dict(strategy_version=VERSION, mode='SHADOW', ready=False,
                  technical_candidate=False, paper_candidate=False, score=0,
                  policy=asdict(policy), evaluated_ms=now_ms)
    if len(points) < 3:
        return dict(result, reason='NEED_THREE_INTRADAY_OBSERVATIONS')
    points = points[-3:]
    last = points[-1]
    identity = ('strategy_version', 'venue', 'symbol', 'day_ms', 'baseline_ms', 'baseline')
    if any(any(p.get(k) != last.get(k) for k in identity) for p in points):
        return dict(result, reason='MIXED_BASELINE_OR_MARKET')
    if last['strategy_version'] != VERSION or not 0 <= now_ms - last['observed_ms'] <= policy.max_age_ms:
        return dict(result, reason='STALE_OR_FUTURE_OBSERVATION')
    if any(not policy.min_gap_ms <= b['observed_ms'] - a['observed_ms'] <= policy.max_gap_ms
           for a, b in zip(points, points[1:])):
        return dict(result, reason='INVALID_OBSERVATION_INTERVAL')
    if not all(isinstance(p[part].get(k), (float, int)) and math.isfinite(p[part][k])
               for p in points for part in ('current', 'baseline') for k in KEYS):
        return dict(result, reason='INVALID_INDICATORS')
    if any(b.get('current_volume', 0) < a.get('current_volume', 0)
           for a, b in zip(points, points[1:])):
        return dict(result, reason='CUMULATIVE_VOLUME_REVISED')
    velocity, acceleration = derivatives(points)
    delta = {k: last['current'][k] - last['baseline'][k] for k in KEYS}
    rising = {k: delta[k] >= d and velocity[k] >= v
              for k, d, v in zip(KEYS, policy.delta_min, policy.velocity_min)}
    accelerating = {k: rising[k] and acceleration[k] >= threshold
                    for k, threshold in zip(KEYS, policy.acceleration_min)}
    score = sum(25 if accelerating[k] else 10 if rising[k] else 0 for k in KEYS)
    exhausted = last['current']['rsi'] >= 78 or last['current']['williams'] >= -8
    candidate = bool(score >= policy.min_score and sum(rising.values()) >= policy.min_rising
                     and sum(accelerating.values()) >= policy.min_accelerating
                     and rising['volume_pace'] and not exhausted)
    confirmed = flow_confirmed(flow, now_ms)
    return dict(result, ready=True, reason='EVALUATED', technical_candidate=candidate,
                paper_candidate=candidate and confirmed, score=score, delta=delta,
                velocity_per_minute=velocity, acceleration_per_minute2=acceleration,
                rising=rising, accelerating=accelerating, exhausted=exhausted,
                flow_confirmed=confirmed, flow=flow, latest=last,
                # Retain all three observations to make every decision reproducible.
                observations=points)
