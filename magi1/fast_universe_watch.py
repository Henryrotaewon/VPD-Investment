"""Add newly listed KRW markets without restarting existing public feeds.

Discovery is independent of FAST entry enablement. Existing warmup rules still
own strategy eligibility. Failed/empty listings never remove an active stream.
"""
import asyncio
import json
import re
import aiohttp

INTERVAL = 30
GROUP_SIZE = 100


def parse_markets(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError('INVALID_MARKET_LIST')
    result = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('market'), str):
            raise ValueError('INVALID_MARKET_ROW')
        market = row['market']
        if market.startswith('KRW-'):
            if not re.fullmatch(r'KRW-[A-Z0-9]+', market):
                raise ValueError('INVALID_KRW_MARKET')
            result.add(market)
    if not result:
        raise ValueError('EMPTY_KRW_UNIVERSE')
    return sorted(result)


async def fetch_markets(session):
    async with session.get('https://api.upbit.com/v1/market/all',
                           params={'is_details': 'true'},
                           timeout=aiohttp.ClientTimeout(total=15)) as response:
        response.raise_for_status()
        return parse_markets(await response.json())


class UniverseWatch:
    def __init__(self, observer, symbols, stream, clock, emit=None):
        self.observer = observer
        # Shared mutable list: Bollinger and FAST-BEAR warmers see additions.
        self.symbols = symbols
        self.stream = stream
        self.clock = clock
        self.emit = emit or (lambda row: print(json.dumps(row), flush=True))
        self.tasks = {}
        previous = observer.db.execute(
            "SELECT value FROM meta WHERE key='observed_krw_universe'").fetchone()
        if previous is None:
            previous = observer.db.execute(
                "SELECT payload FROM events WHERE kind='UNIVERSE' ORDER BY id DESC LIMIT 1").fetchone()
            self.previous = set(json.loads(previous[0]).get('symbols', [])) if previous else set(symbols)
        else:
            self.previous = set(json.loads(previous[0]))
        observer.universe_status = dict(refresh_seconds=INTERVAL, status='STARTING',
            last_success_ms=None, listed=len(symbols), subscribed=0, recent_added=[])

    async def subscribe(self, names):
        for i in range(0, len(names), GROUP_SIZE):
            group = 'universe-' + str(len(self.tasks))
            batch = tuple(names[i:i+GROUP_SIZE])
            self.tasks[group] = asyncio.create_task(self.stream(group, batch))
            # Keep new WebSocket attempts below the connection rate limit.
            await asyncio.sleep(.3)

    def record(self, listed, added, phase):
        ts = self.clock()
        state = self.observer.universe_status
        state.update(status='OK', last_success_ms=ts, listed=len(listed),
                     subscribed=len(self.symbols), retained_absent=sorted(set(self.symbols)-set(listed)))
        if added:
            state['recent_added'] = (state['recent_added'] + list(added))[-10:]
            self.observer.event(ts, 'UNIVERSE_ADDED', '', dict(symbols=added, phase=phase))
            self.emit(dict(mode='FAST_UNIVERSE_ADDED', ts=ts, symbols=added,
                           phase=phase, fast_disabled=self.observer.fast_disabled,
                           subscribed=len(self.symbols)))
        self.observer.db.execute("INSERT OR REPLACE INTO meta VALUES('observed_krw_universe',?)",
                                 (json.dumps(self.symbols),))
        self.observer.db.commit()

    async def start(self):
        await self.subscribe(self.symbols)
        self.record(self.symbols, sorted(set(self.symbols)-self.previous), 'STARTUP')
        self.observer.event(self.clock(), 'UNIVERSE', '',
                            dict(symbols=self.symbols, scope='ALL_KRW_OBSERVATION'))
        self.observer.db.commit()

    async def refresh(self, listed):
        # Caller has validated the complete REST response before any mutation.
        added = sorted(set(listed)-set(self.symbols))
        if added:
            self.symbols.extend(added)
            if self.observer.repair:
                self.observer.repair.symbols.extend(added)
                for symbol in added:
                    self.observer.repair.gate(symbol)
                self.observer.repair.wake.set()
            await self.subscribe(added)
        self.record(listed, added, 'REFRESH')
        return added

    async def check(self, session):
        try:
            listed = await fetch_markets(session)
        except Exception as exc:
            self.observer.universe_status.update(status='RETRY', error=type(exc).__name__)
            self.observer.event(self.clock(), 'UNIVERSE_REFRESH_ERROR', '',
                                dict(type=type(exc).__name__))
            self.observer.db.commit()
            self.emit(dict(mode='FAST_UNIVERSE_REFRESH_ERROR', type=type(exc).__name__))
            return
        await self.refresh(listed)
        self.observer.universe_status.pop('error', None)

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def run(self, session):
        try:
            await self.start()
            while True:
                await asyncio.sleep(INTERVAL)
                for task in self.tasks.values():
                    if task.done():
                        task.result()
                        raise RuntimeError('UNIVERSE_STREAM_STOPPED')
                await self.check(session)
        finally:
            await self.close()
