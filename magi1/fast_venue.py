"""Additional venue-local FAST workers; public spot data and PAPER only.

The existing Upbit worker is unchanged. Each extra venue owns its connection,
turnover history and account. No cross-venue prices enter a spot signal.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
import zlib
from zoneinfo import ZoneInfo

import aiohttp
import requests
from magi1.fast_observe import Observer, FIVE, DAY, now_ms
from magi1.fast_paper import Paper, POLICY
from magi1.fast_repair import Repair
from magi1.fast_universe_watch import UniverseWatch, parse_markets
from magi1.fast_candle_store import enable, compact

VENUES = ('bithumb', 'binance', 'kraken')
CURRENCY = {'bithumb': 'KRW', 'binance': 'USDT', 'kraken': 'USD'}
FEES = {'bithumb': .0025, 'binance': .001, 'kraken': .004}
STABLE = {'USDT', 'USDC', 'DAI', 'TUSD', 'FDUSD', 'USDP', 'USD1', 'EUR', 'KRW', 'USD'}


def iso_ms(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)


async def get_json(session, url, params=None):
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        data = await r.json()
    if isinstance(data, dict) and data.get('error'):
        raise ValueError('PUBLIC_API_REJECTED')
    return data


async def discover(session, venue):
    if venue == 'bithumb':
        rows = parse_markets(await get_json(session, 'https://api.bithumb.com/v1/market/all'))
        # Startup priority, not a universe cap. Q receives the first connection
        # and historical repair; every listed KRW market remains subscribed.
        return sorted(rows, key=lambda s: (s != 'KRW-Q', s))
    if venue == 'binance':
        data = await get_json(session, 'https://api.binance.com/api/v3/exchangeInfo')
        rows = [r['symbol'] for r in data['symbols'] if r['quoteAsset'] == 'USDT'
                and r['baseAsset'] not in STABLE and r['status'] == 'TRADING'
                and r.get('isSpotTradingAllowed', False)]
    else:
        # v2 instrument symbols are authoritative (BTC/USD, not v1 XBT/USD).
        async with session.ws_connect('wss://ws.kraken.com/v2', heartbeat=20) as ws:
            await ws.send_json(dict(method='subscribe', params=dict(channel='instrument', snapshot=True)))
            async with asyncio.timeout(20):
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(message.data)
                    if data.get('success') is False:
                        raise ValueError('INSTRUMENT_SUBSCRIPTION_REJECTED')
                    if data.get('channel') == 'instrument' and data.get('type') == 'snapshot':
                        rows = [p['symbol'] for p in data['data']['pairs']
                                if p['quote'] == 'USD' and p['base'] not in STABLE
                                and p['status'] == 'online']
                        break
                else:
                    raise ValueError('INSTRUMENT_STREAM_ENDED')
    if not rows or len(rows) != len(set(rows)):
        raise ValueError('INVALID_VENUE_UNIVERSE')
    return sorted(rows)


def standard_candle(t, o, h, l, c, volume, value):
    return dict(candle_date_time_utc=datetime.fromtimestamp(t / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
                opening_price=float(o), high_price=float(h), low_price=float(l),
                trade_price=float(c), candle_acc_trade_volume=float(volume), candle_acc_trade_price=float(value))


class CandleClient:
    """Validated native turnover; Kraken's 720-candle limit remains explicit."""
    def __init__(self, venue):
        self.venue = venue
        self.session = requests.Session()
        self.next = 0.
        self.kraken_pairs = {}

    def get(self, url, params):
        time.sleep(max(0, self.next - time.monotonic()))
        self.next = time.monotonic() + (1.1 if self.venue == 'kraken' else .4)
        response = self.session.get(url, params=params, timeout=(3, 12))
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get('error'):
            raise ValueError('PUBLIC_API_REJECTED')
        return data

    def candles(self, symbol, cursor):
        if self.venue == 'bithumb':
            # Bithumb defines `to` as an exclusive KST wall-clock boundary;
            # unlike Upbit it rejects a UTC Z-suffixed value.
            to = datetime.fromtimestamp(cursor / 1000, ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%dT%H:%M:%S')
            return self.get('https://api.bithumb.com/v1/candles/minutes/5',
                            dict(market=symbol, to=to, count=200))
        if self.venue == 'binance':
            rows = self.get('https://api.binance.com/api/v3/klines',
                            dict(symbol=symbol, interval='5m', endTime=cursor - 1, limit=200))
            return [standard_candle(int(r[0]), *r[1:6], r[7]) for r in rows if int(r[0]) + FIVE <= cursor]
        if symbol not in self.kraken_pairs:
            pairs=self.get('https://api.kraken.com/0/public/AssetPairs',{})
            for item in pairs['result'].values():
                name=item.get('wsname','').split('/')
                if len(name)==2:
                    base={'XBT':'BTC','XDG':'DOGE'}.get(name[0],name[0])
                    self.kraken_pairs[base+'/'+name[1]]=item['altname']
        if symbol not in self.kraken_pairs:raise ValueError('REST_PAIR_METADATA_UNAVAILABLE')
        data = self.get('https://api.kraken.com/0/public/OHLC', dict(pair=self.kraken_pairs[symbol], interval=5,
                        since=max(0, cursor // 1000 - 720 * 300)))
        rows = next(v for k, v in data['result'].items() if k != 'last')
        # The exchange cannot serve older history. Do not invent zero turnover
        # before the oldest returned candle or shorten the ten-day reference.
        return [standard_candle(int(r[0]) * 1000, *r[1:5], r[6], float(r[5]) * float(r[6]))
                for r in rows if int(r[0]) * 1000 + FIVE <= cursor][-200:]


def book_message(symbol, bids, asks, stamp, **extra):
    count = min(len(bids), len(asks))
    if not count:
        raise ValueError('EMPTY_BOOK')
    return dict(type='orderbook', code=symbol, timestamp=stamp,
                orderbook_units=[dict(bid_price=float(bids[i][0]), bid_size=float(bids[i][1]),
                                     ask_price=float(asks[i][0]), ask_size=float(asks[i][1])) for i in range(count)], **extra)


class Decoder:
    def __init__(self, venue):
        self.venue = venue
        self.books = {}
        self.sequences = {}

    def decode(self, body, received):
        if body.get('error') or body.get('success') is False:
            raise ValueError('SUBSCRIPTION_REJECTED')
        if self.venue == 'bithumb':
            if body.get('type') in ('trade', 'orderbook'):
                # Some native timestamps use microseconds; normalize once.
                body = dict(body)
                for field in ('trade_timestamp', 'timestamp'):
                    if int(body.get(field, 0)) > 10**14:
                        body[field] = int(body[field]) // 1000
                return [body]
            return []
        if self.venue == 'binance':
            row = body.get('data', body)
            if row.get('e') == 'trade':
                return [dict(type='trade', code=row['s'], trade_timestamp=int(row['T']),
                    sequential_id=row['t'], trade_price=float(row['p']), trade_volume=float(row['q']),
                    ask_bid='ASK' if row['m'] else 'BID', stream_type='REALTIME')]
            if 'lastUpdateId' in row and 'bids' in row:
                symbol = body['stream'].split('@')[0].upper()
                sequence = int(row['lastUpdateId'])
                if sequence <= self.sequences.get(symbol, -1):
                    return []
                self.sequences[symbol] = sequence
                # Partial-depth streams have no exchange timestamp. Arrival
                # age is explicit; sequence monotonicity is checked separately.
                return [book_message(symbol, row['bids'], row['asks'], received,
                                     timestamp_basis='RECEIVE_TIME', sequence=sequence)]
            return []
        channel = body.get('channel')
        if channel == 'trade':
            if body.get('type') != 'update':
                return []
            return [dict(type='trade', code=r['symbol'], trade_timestamp=iso_ms(r['timestamp']),
                    sequential_id=r['trade_id'], trade_price=float(r['price']), trade_volume=float(r['qty']),
                    ask_bid={'buy': 'BID', 'sell': 'ASK'}[r['side']], stream_type='REALTIME')
                    for r in body['data']]
        result = []
        if channel == 'book':
            for row in body['data']:
                symbol = row['symbol']
                if body['type'] == 'snapshot':
                    self.books[symbol] = {'bids': {}, 'asks': {}}
                state = self.books.get(symbol)
                if state is None:
                    raise ValueError('BOOK_UPDATE_BEFORE_SNAPSHOT')
                for side in ('bids', 'asks'):
                    for item in row.get(side, []):
                        p, q = Decimal(str(item['price'])), Decimal(str(item['qty']))
                        if q == 0:
                            state[side].pop(p, None)
                        else:
                            state[side][p] = q
                    state[side] = dict(sorted(state[side].items(), reverse=side == 'bids')[:10])
                def digits(value):
                    return format(value, 'f').replace('.', '').lstrip('0')
                encoded = ''.join(digits(p) + digits(q) for side in ('asks', 'bids') for p, q in state[side].items())
                if zlib.crc32(encoded.encode()) != int(row['checksum']):
                    raise ValueError('BOOK_CHECKSUM_MISMATCH')
                result.append(book_message(symbol, list(state['bids'].items()), list(state['asks'].items()), iso_ms(row['timestamp'])))
        return result


async def stream(session, observer, venue, group, names):
    backoff = 1
    while True:
        try:
            decoder = Decoder(venue)
            if venue == 'bithumb':
                url = 'wss://ws-api.bithumb.com/websocket/v1'
            elif venue == 'binance':
                channels = '/'.join(s.lower() + suffix for s in names for suffix in ('@trade', '@depth5'))
                url = 'wss://stream.binance.com:443/stream?streams=' + channels
            else:
                url = 'wss://ws.kraken.com/v2'
            async with session.ws_connect(url, heartbeat=20, receive_timeout=45) as ws:
                if venue == 'bithumb':
                    await ws.send_json([dict(ticket=uuid.uuid4().hex), dict(type='trade', codes=list(names), is_only_realtime=True),
                                        dict(type='orderbook', codes=list(names)), dict(format='DEFAULT')])
                elif venue == 'kraken':
                    await ws.send_json(dict(method='subscribe', params=dict(channel='trade', symbol=list(names), snapshot=False)))
                    await ws.send_json(dict(method='subscribe', params=dict(channel='book', symbol=list(names), depth=10, snapshot=True)))
                observer.connected(group, names, now_ms())
                backoff = 1
                async for message in ws:
                    if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                        continue
                    body = json.loads(message.data, parse_float=Decimal) if venue == 'kraken' else json.loads(message.data)
                    received = now_ms()
                    for row in decoder.decode(body, received):
                        if row.get('code') not in names:
                            raise ValueError('UNSUBSCRIBED_SYMBOL')
                        try:
                            observer.ingest(row, received)
                        except (ValueError, TypeError, KeyError, IndexError):
                            observer.invalid_symbol(row['code'], received)
                raise ConnectionError('STREAM_CLOSED')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            observer.gap(group, now_ms(), type(exc).__name__ + ':' + str(exc)[:100])
            print(json.dumps(dict(mode='FAST_VENUE_FEED_RETRY', venue=venue, group=group,
                                  reason=type(exc).__name__ + ':' + str(exc)[:120])), flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def fx_quote(session, currency):
    if currency == 'KRW':
        return dict(bid=1., ask=1., ts=now_ms())
    rows = await get_json(session, 'https://api.upbit.com/v1/orderbook', dict(markets='KRW-USDT'))
    book = rows[0]
    if abs(now_ms() - int(book['timestamp'])) > 15000:
        raise ValueError('STALE_FX')
    u = book['orderbook_units'][0]
    bid, ask = float(u['bid_price']), float(u['ask_price'])
    if currency == 'USD':
        data = await get_json(session, 'https://api.kraken.com/0/public/Depth', dict(pair='USDTUSD', count=1))
        depth = next(iter(data['result'].values()))
        bid /= float(depth['asks'][0][0])
        ask /= float(depth['bids'][0][0])
    if not 0 < bid <= ask or not math.isfinite(ask):
        raise ValueError('INVALID_FX')
    return dict(bid=bid, ask=ask, ts=now_ms())


def account(path, stamp, venue, fx):
    policy = None
    if Path(path).exists():
        with sqlite3.connect(path) as db:
            row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            if row:
                policy = json.loads(row[0])['policy']
    if policy is None:
        policy = dict(POLICY, version=POLICY['version'] + '-' + venue, venue=venue,
                      currency=CURRENCY[venue], initial=3000000. / fx['ask'],
                      initial_krw=3000000., initial_fx_krw=fx['ask'], fee=FEES[venue],
                      min_budget=5000 if venue == 'bithumb' else 5.)
    return Paper(path, stamp, policy=policy)


class VenueObserver(Observer):
    def __init__(self, path, start, venue):
        super().__init__(path, start)
        self.venue = venue
        enable(self.db)

    def event(self, ts, kind, symbol, payload):
        if kind=='ASSESSMENT':
            payload={k:payload[k] for k in ('reasons','qualifies','fresh','enough','relative_value','buy_share','burst','status') if k in payload}
        super().event(ts,kind,symbol,payload)

    def evaluate(self, end):
        super().evaluate(end)

    def report(self):
        result = super().report()
        result.update(venue=self.venue, currency=CURRENCY[self.venue],
            history_note='최근 720봉만 조회 가능 · 10일 동일시간 자료 축적 대기' if self.venue == 'kraken' else '거래소별 10일 동일시간 비교',
            trade_received=len(self.last_price), book_received=len(self.books),
            ready_symbols=max(0, len(self.symbol_group) - len(self.repair_blocked)),
            paper_policy=self.paper.policy if self.paper else None,
            book_timestamp_basis='RECEIVE_TIME_AND_SEQUENCE' if self.venue=='binance' else 'EXCHANGE_TIME',
            focus={s: dict(reference=self.reference_status.get(s), trade_ms=self.last_price.get(s, (None,))[0],
                          book_ms=self.books.get(s, {}).get('ts')) for s in ('KRW-Q',) if s in self.symbol_group})
        return result


async def run(args):
    observer = VenueObserver(args.db, now_ms(), args.venue)
    observer_path = Path(args.db)
    tasks = []
    last_report = 0.
    fx = None
    async with aiohttp.ClientSession() as session:
        try:
            symbols = await discover(session, args.venue)
            if Path(args.paper_db).exists():
                # Existing native-currency balances do not depend on a new FX
                # quote to resume feed protection and liquidation tracking.
                observer.paper = account(args.paper_db, now_ms(), args.venue, {'ask':1.})
                observer.paper.diagnostics = True
                if observer.fast_disabled:observer.paper.pause_entries(now_ms())
            repair = Repair(observer, symbols, observer_path, client=CandleClient(args.venue))
            tasks.append(asyncio.create_task(repair.run()))
            watcher = UniverseWatch(observer, symbols,
                lambda group, names: stream(session, observer, args.venue, group, names),
                now_ms, fetcher=lambda s: discover(s, args.venue))
            tasks.append(asyncio.create_task(watcher.run(session)))
            stop = time.monotonic() + args.duration
            while args.continuous or time.monotonic() < stop:
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError('VENUE_WORKER_STOPPED')
                observer.advance(now_ms())
                if time.monotonic() - last_report >= 60:
                    last_report = time.monotonic()
                    try:
                        fx = await fx_quote(session, CURRENCY[args.venue])
                        if observer.paper is None:
                            observer.paper=account(args.paper_db,now_ms(),args.venue,fx)
                            observer.paper.diagnostics=True
                            if observer.fast_disabled:observer.paper.pause_entries(now_ms())
                    except Exception:
                        pass  # retain the actual timestamp; stale FX is labelled
                    report = observer.report()
                    report['fx'] = fx
                    print(json.dumps(report, allow_nan=False), flush=True)
                await asyncio.sleep(.2)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            observer.db.commit()
            observer.db.close()
            if observer.paper:
                observer.paper.close(now_ms())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venue', choices=VENUES, required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--paper-db', required=True)
    parser.add_argument('--continuous', action='store_true')
    parser.add_argument('--duration', type=int, default=3600)
    args = parser.parse_args()
    if not 1 <= args.duration <= 86400:
        parser.error('duration must be 1..86400')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
