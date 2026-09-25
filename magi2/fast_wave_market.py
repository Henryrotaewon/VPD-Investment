"""Public REST adapters for the four FAST spot venues; no account/order endpoints."""
from datetime import datetime, timezone
from magi2.fast_monitor import PublicMarket, now
from magi2.fast_wave_indicator import Candle, DAY_MS, snapshot, day_offset, day_start


def normalize(venue, payload):
    if venue in ('upbit', 'bithumb'):
        rows = [Candle(int(datetime.fromisoformat(x['candle_date_time_utc']).replace(
            tzinfo=timezone.utc).timestamp() * 1000), float(x['high_price']),
            float(x['low_price']), float(x['trade_price']), float(x['candle_acc_trade_volume']))
            for x in payload]
    elif venue == 'binance':
        rows = [Candle(int(x[0]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in payload]
    elif venue == 'kraken':
        if payload.get('error'):
            raise ValueError('PUBLIC_API_REJECTED')
        series = [x for k, x in payload['result'].items() if k != 'last']
        if len(series) != 1:
            raise ValueError('AMBIGUOUS_OHLC_PAIR')
        rows = [Candle(int(x[0]) * 1000, float(x[2]), float(x[3]), float(x[4]), float(x[6]))
                for x in series[0]]
    else:
        raise ValueError('INVALID_VENUE')
    for row in rows:
        row.validate(day_offset(venue))
    if len({row.open_ms for row in rows}) != len(rows):
        raise ValueError('DUPLICATE_CANDLE')
    return sorted(rows, key=lambda row: row.open_ms)


class IndicatorMarket(PublicMarket):
    def __init__(self, venue, clock=now):
        super().__init__(venue)
        self.clock = clock
        self.cache = {}

    def observe(self, symbol):
        # Discover an eligible spot universe first: do not accept arbitrary symbols.
        if symbol not in self.symbols:
            raise ValueError('SYMBOL_OUTSIDE_SPOT_UNIVERSE')
        started = self.clock()
        day = day_start(self.venue, started)
        cached = self.cache.get(symbol)
        warm = cached is not None and cached[0] == day
        count = 2 if warm else 121
        if self.venue in ('upbit', 'bithumb'):
            payload = self.get('/v1/candles/days', {'market': symbol, 'count': count})
        elif self.venue == 'binance':
            payload = self.get('/api/v3/klines', {'symbol': symbol, 'interval': '1d', 'limit': count, 'timeZone': '0'})
        else:
            payload = self.get('/0/public/OHLC', {'pair': symbol, 'interval': 1440,
                               'since': (day - (1 if warm else 120) * DAY_MS) // 1000})
        finished = self.clock()
        if not 0 <= finished - started <= 3000 or day_start(self.venue, finished) != day:
            raise ValueError('SLOW_OR_CROSS_DAY_OHLC')
        rows = normalize(self.venue, payload)
        if any(row.open_ms > day for row in rows):
            raise ValueError('FUTURE_CANDLE')
        current = [row for row in rows if row.open_ms == day]
        if len(current) != 1:
            raise ValueError('CURRENT_DAY_CANDLE_MISSING')
        completed = cached[1] if warm else [row for row in rows if row.open_ms < day][-120:]
        if warm:
            # Closed history is frozen within a day; revisions reset the warmup.
            previous = [row for row in rows if row.open_ms == day - DAY_MS]
            if not previous or previous[0] != completed[-1]:
                self.cache.pop(symbol, None)
                raise ValueError('BASELINE_REVISED_REWARM_REQUIRED')
        point = snapshot(completed, current[0], finished, venue=self.venue, symbol=symbol)
        self.cache[symbol] = (day, completed)
        return point

    def flow(self, symbol):
        started = self.clock()
        result = self.buying_flow(symbol)
        quotes = self.quotes([symbol])
        finished = self.clock()
        if not 0 <= finished - started <= 1500:
            raise ValueError('SLOW_FLOW_CONFIRMATION')
        bid, ask = quotes[symbol]
        if not 0 < bid <= ask:
            raise ValueError('INVALID_QUOTE')
        # Conservatively age the sample from the request start, not after quote fetch.
        return dict(result, observed_ms=started, spread_bps=(ask / bid - 1) * 10000)
