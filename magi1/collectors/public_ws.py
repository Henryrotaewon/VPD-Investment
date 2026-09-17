"""Public-only spot adapters with measurement-grade diagnostics.

Cross-venue lead/lag research uses received_ts_ms as the primary comparable
clock because every venue is observed on the same Railway process. Exchange
timestamps are retained as a secondary quality signal; they are not treated as
perfectly synchronized latency clocks.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import random
import uuid
from collections import Counter, deque
from datetime import datetime
from decimal import Decimal
import zlib
import aiohttp
from ..config import BOOK_DEPTH, STALE_FEED_SEC
from ..normalizer import book, now_ms, trade

LOG = logging.getLogger(__name__)
URLS = {
    'binance': 'wss://stream.binance.com:9443/stream',
    'bybit': 'wss://stream.bybit.com/v5/public/spot',
    'kraken': 'wss://ws.kraken.com/v2',
    'upbit': 'wss://api.upbit.com/websocket/v1',
    'bithumb': 'wss://ws-api.bithumb.com/websocket/v1',
    'coinone': 'wss://stream.coinone.co.kr',
}

# Binance partial-depth snapshots do not carry exchange event time. This is a
# protocol characteristic, not a collection defect. Keep the book for flow
# state, but never use it as an exchange-clock latency observation.
EXPECTED_MISSING_TS = {('binance', 'BookEvent')}
BOOK_STALE_MS = max(5_000, STALE_FEED_SEC * 1000)
TRADE_IDLE_MS = max(300_000, STALE_FEED_SEC * 10_000)
MESSAGE_STALE_MS = max(30_000, STALE_FEED_SEC * 2_000)


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, str) and 'T' in value:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
    return int(value)


def _percentile(values, q):
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return round(xs[lo] * (1 - frac) + xs[hi] * frac, 3)


class Adapter:
    def __init__(self, venue):
        self.venue = venue
        self.books = {}
        self.sequences = {}

    def url(self, assets):
        if self.venue == 'binance':
            streams = '/'.join(
                f'{a.lower()}usdt@trade/{a.lower()}usdt@depth10@100ms'
                for a in assets
            )
            return URLS[self.venue] + '?streams=' + streams
        return URLS[self.venue]

    def subscriptions(self, assets):
        if self.venue == 'binance':
            return []
        if self.venue == 'bybit':
            return [{'op': 'subscribe', 'args': [
                s for a in assets for s in
                (f'publicTrade.{a}USDT', f'orderbook.50.{a}USDT')
            ]}]
        if self.venue == 'kraken':
            return [
                {'method': 'subscribe',
                 'params': dict(channel=ch,
                                symbol=[f'{a}/USD' for a in assets],
                                **extra)}
                for ch, extra in
                [('trade', {'snapshot': False}),
                 ('book', {'depth': 10, 'snapshot': True})]
            ]
        if self.venue in ('upbit', 'bithumb'):
            return [[
                {'ticket': str(uuid.uuid4())},
                {'type': 'trade',
                 'codes': [f'KRW-{a}' for a in assets],
                 'is_only_realtime': True},
                {'type': 'orderbook',
                 'codes': [f'KRW-{a}' for a in assets]},
                {'format': 'DEFAULT'},
            ]]
        return [
            {'request_type': 'SUBSCRIBE',
             'channel': ch,
             'topic': {'quote_currency': 'KRW', 'target_currency': a}}
            for a in assets for ch in ('TRADE', 'ORDERBOOK')
        ]

    def subscribe(self, assets):
        return self.subscriptions(assets)

    def incremental(self, symbol, bids, asks, snapshot, sequence=None,
                    depth=50, checksum=None):
        if snapshot:
            self.books[symbol] = ({}, {})
            self.sequences.pop(symbol, None)
        if symbol not in self.books:
            raise ValueError(f'{self.venue} delta before snapshot: {symbol}')
        old = self.sequences.get(symbol)
        if sequence is not None and old is not None and int(sequence) <= old:
            return None
        sides = self.books[symbol]
        for side, rows in zip(sides, (bids, asks)):
            for p, q in rows:
                p, q = Decimal(str(p)), Decimal(str(q))
                if q == 0:
                    side.pop(p, None)
                else:
                    side[p] = q
        for i, side in enumerate(sides):
            for p in sorted(side, reverse=i == 0)[depth:]:
                del side[p]
        if checksum is not None:
            def digits(n):
                return format(n, 'f').replace('.', '').lstrip('0')
            s = ''.join(
                digits(p) + digits(q)
                for i in (1, 0)
                for p, q in sorted(
                    sides[i].items(), reverse=i == 0
                )[:10]
            )
            if zlib.crc32(s.encode()) != int(checksum):
                raise ValueError(f'Kraken checksum mismatch: {symbol}')
        if sequence is not None:
            self.sequences[symbol] = int(sequence)
        return [
            sorted(sides[0].items(), reverse=True),
            sorted(sides[1].items())
        ]

    def parse(self, m, received):
        v = self.venue
        if (m.get('error') or m.get('success') is False
                or m.get('response_type') == 'ERROR'):
            raise ValueError(f'{v} subscription error: {m}')
        if v == 'binance':
            d = m.get('data', m)
            symbol = d.get('s', m.get('stream', '').split('@')[0])
            if d.get('e') == 'trade':
                return [trade(v, symbol, d['p'], d['q'], d.get('T'),
                              'SELL' if d['m'] else 'BUY',
                              d.get('t'), received)]
            if 'lastUpdateId' in d:
                return [book(v, symbol, d['bids'], d['asks'], None,
                             d['lastUpdateId'], received)]
        elif v == 'bybit':
            d = m.get('data')
            topic = m.get('topic', '')
            if topic.startswith('publicTrade'):
                return [
                    trade(v, x['s'], x['p'], x['v'], x['T'], x['S'],
                          x.get('i'), received)
                    for x in d
                ]
            if topic.startswith('orderbook'):
                levels = self.incremental(
                    d['s'], d['b'], d['a'],
                    m.get('type') == 'snapshot' or d.get('u') == 1,
                    d['u'])
                if levels:
                    return [book(v, d['s'], *levels,
                                 m.get('cts', m.get('ts')),
                                 d['u'], received)]
        elif v == 'kraken':
            ch = m.get('channel')
            data = m.get('data', [])
            if ch == 'trade' and m.get('type') != 'snapshot':
                return [
                    trade(v, x['symbol'], x['price'], x['qty'],
                          timestamp(x['timestamp']), x['side'],
                          x.get('trade_id'), received)
                    for x in data
                ]
            if ch == 'book':
                out = []
                for d in data:
                    levels = self.incremental(
                        d['symbol'],
                        [(x['price'], x['qty']) for x in d.get('bids', [])],
                        [(x['price'], x['qty']) for x in d.get('asks', [])],
                        m.get('type') == 'snapshot',
                        depth=10, checksum=d.get('checksum'))
                    if levels:
                        out.append(book(
                            v, d['symbol'], *levels,
                            timestamp(d.get('timestamp')),
                            d.get('checksum'), received))
                return out
        elif v in ('upbit', 'bithumb'):
            if m.get('type') == 'trade':
                return [trade(
                    v, m['code'], m['trade_price'], m['trade_volume'],
                    m.get('trade_timestamp'),
                    'BUY' if m['ask_bid'] == 'BID' else 'SELL',
                    m.get('sequential_id'), received)]
            if m.get('type') == 'orderbook':
                rows = m['orderbook_units']
                ts = m.get('timestamp')
                if v == 'bithumb' and ts is not None:
                    ts = int(ts) // 1000
                return [book(
                    v, m['code'],
                    [(x['bid_price'], x['bid_size']) for x in rows],
                    [(x['ask_price'], x['ask_size']) for x in rows],
                    ts, None, received)]
        elif v == 'coinone' and m.get('response_type') == 'DATA':
            d = m['data']
            symbol = f"{d['target_currency']}-{d['quote_currency']}"
            if m.get('channel') == 'TRADE':
                side = (
                    'BUY' if d.get('is_seller_maker') is True
                    else 'SELL' if d.get('is_seller_maker') is False
                    else None
                )
                return [trade(v, symbol, d['price'], d['qty'],
                              d.get('timestamp'), side, d.get('id'),
                              received)]
            if m.get('channel') == 'ORDERBOOK':
                return [book(
                    v, symbol,
                    [(x['price'], x['qty']) for x in d['bids']],
                    [(x['price'], x['qty']) for x in d['asks']],
                    d.get('timestamp'), d.get('id'), received)]
        return []


VENUE_ADAPTERS = {v: Adapter(v) for v in URLS}


class CollectorSupervisor:
    def __init__(self, symbols, sink):
        self.symbols = symbols
        self.sink = sink
        self.last_message = {}
        self.last_event = {}
        self.counts = Counter()
        self.reconnects = Counter()
        self.offsets = {
            (v, kind): deque(maxlen=2000)
            for v in URLS
            for kind in ('TradeEvent', 'BookEvent')
        }
        self.missing_ts = Counter()
        self.expected_missing_ts = Counter()
        self.unexpected_missing_ts = Counter()
        self.timestamp_regressions = Counter()
        self.last_exchange = {}
        self.connected = {}
        self.parse_errors = Counter()

    async def heartbeat(self, ws, v):
        while True:
            await asyncio.sleep(15)
            if v == 'bybit':
                await ws.send_json({'op': 'ping'})
            elif v == 'coinone':
                await ws.send_json({'request_type': 'PING'})
            else:
                await ws.ping()

            # Connection liveness is message-based. Sparse trades must not
            # trigger reconnects while the book stream is healthy.
            last = self.last_message.get(v)
            if last is not None and now_ms() - last > MESSAGE_STALE_MS:
                await ws.close()
                return

    async def run_venue(self, session, venue):
        attempt = 0
        while True:
            started = now_ms()
            adapter = Adapter(venue)
            try:
                async with session.ws_connect(
                    adapter.url(self.symbols),
                    heartbeat=20,
                    receive_timeout=45
                ) as ws:
                    self.connected[venue] = True
                    for payload in adapter.subscriptions(self.symbols):
                        await ws.send_json(payload)
                    heartbeat = asyncio.create_task(
                        self.heartbeat(ws, venue))
                    try:
                        async for msg in ws:
                            if msg.type not in (
                                aiohttp.WSMsgType.TEXT,
                                aiohttp.WSMsgType.BINARY
                            ):
                                if msg.type in (
                                    aiohttp.WSMsgType.ERROR,
                                    aiohttp.WSMsgType.CLOSED
                                ):
                                    break
                                continue
                            received = now_ms()
                            self.last_message[venue] = received
                            try:
                                m = (
                                    json.loads(msg.data, parse_float=Decimal)
                                    if venue == 'kraken'
                                    else json.loads(msg.data)
                                )
                                events = adapter.parse(m, received)
                            except (ValueError, KeyError, TypeError):
                                self.parse_errors[venue] += 1
                                raise
                            for event in events:
                                kind = event.__class__.__name__
                                key = (venue, event.base, kind)
                                if not self.counts[key]:
                                    LOG.info(
                                        'first_event venue=%s asset=%s type=%s',
                                        *key)
                                self.counts[key] += 1
                                self.last_event[key] = received
                                if event.exchange_ts_ms is None:
                                    self.missing_ts[venue] += 1
                                    if (venue, kind) in EXPECTED_MISSING_TS:
                                        self.expected_missing_ts[venue] += 1
                                    else:
                                        self.unexpected_missing_ts[venue] += 1
                                else:
                                    self.offsets[(venue, kind)].append(
                                        received - event.exchange_ts_ms)
                                    previous = self.last_exchange.get(key)
                                    if (previous is not None and
                                            event.exchange_ts_ms < previous):
                                        self.timestamp_regressions[venue] += 1
                                    self.last_exchange[key] = (
                                        event.exchange_ts_ms)
                                await self.sink(event)
                            if (now_ms() - started > 60_000 and
                                    not any(k[0] == venue and t >= started
                                            for k, t
                                            in self.last_event.items())):
                                raise TimeoutError(
                                    'connected but no normalized market events')
                    finally:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning('venue=%s error=%r', venue, exc)
            self.connected[venue] = False
            self.reconnects[venue] += 1
            attempt = (
                0 if now_ms() - started > 60_000
                else min(attempt + 1, 5)
            )
            await asyncio.sleep(min(30, 2 ** attempt) + random.random())

    async def run(self):
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)
        ) as session:
            await asyncio.gather(
                *(self.run_venue(session, v) for v in URLS)
            )

    def diagnostics(self):
        result = {}
        current = now_ms()
        for v in URLS:
            feeds = {}
            book_stale = []
            trade_inactive = []
            for a in self.symbols:
                for kind in ('TradeEvent', 'BookEvent'):
                    key = (v, a, kind)
                    age = (
                        current - self.last_event[key]
                        if key in self.last_event else None
                    )
                    threshold = (
                        BOOK_STALE_MS
                        if kind == 'BookEvent'
                        else TRADE_IDLE_MS
                    )
                    state = (
                        'MISSING' if age is None
                        else 'STALE' if age > threshold
                        else 'OK'
                    )
                    feeds[f'{a}/{kind}'] = {
                        'count': self.counts[key],
                        'age_ms': age,
                        'threshold_ms': threshold,
                        'state': state,
                    }
                    if kind == 'BookEvent' and state != 'OK':
                        book_stale.append(a)
                    if kind == 'TradeEvent' and state != 'OK':
                        trade_inactive.append(a)

            timing = {}
            for kind in ('TradeEvent', 'BookEvent'):
                values = list(self.offsets[(v, kind)])
                timing[kind] = {
                    'samples': len(values),
                    'p05_ms': _percentile(values, 0.05),
                    'median_ms': _percentile(values, 0.50),
                    'p95_ms': _percentile(values, 0.95),
                    'spread_p95_p05_ms': (
                        round(_percentile(values, 0.95)
                              - _percentile(values, 0.05), 3)
                        if values else None
                    ),
                    'basis': (
                        'received_ts_ms-exchange_ts_ms'
                        if values else None
                    ),
                }

            message_age = (
                current - self.last_message[v]
                if v in self.last_message else None
            )
            connected = self.connected.get(v, False)
            transport_healthy = (
                connected
                and message_age is not None
                and message_age <= MESSAGE_STALE_MS
            )
            measurement_ready = (
                transport_healthy
                and not book_stale
                and self.unexpected_missing_ts[v] == 0
                and self.parse_errors[v] == 0
            )
            result[v] = {
                'connected': connected,
                'message_age_ms': message_age,
                'feeds': feeds,
                # "stale" now means measurement-critical book/transport stale,
                # not a naturally quiet trade stream.
                'stale': not transport_healthy or bool(book_stale),
                'book_stale_assets': book_stale,
                'trade_inactive_assets': trade_inactive,
                'reconnects': self.reconnects[v],
                'parse_errors': self.parse_errors[v],
                'timestamp_missing': self.missing_ts[v],
                'timestamp_missing_expected': self.expected_missing_ts[v],
                'timestamp_missing_unexpected': (
                    self.unexpected_missing_ts[v]
                ),
                'timestamp_regressions': self.timestamp_regressions[v],
                'timing_quality': timing,
                'measurement_ready': measurement_ready,
                'lead_time_clock': 'received_ts_ms',
                'lead_time_note': (
                    'same-process receive clock is primary; exchange clocks '
                    'are secondary diagnostics and include path latency'
                ),
                'receive_jitter_ms': max(
                    [x['spread_p95_p05_ms'] or 0 for x in timing.values()]
                ),
            }
        return result
