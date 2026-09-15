from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import aiohttp

from ..config import BOOK_DEPTH, RECONNECT_BACKOFF_SEC, STALE_FEED_SEC
from ..normalizer import book, now_ms, trade

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Adapter:
    venue: str
    url: Callable[[list[str]], str]
    subscribe: Callable[[list[str]], Any | None]
    parse: Callable[[Any, int], list[Any]]


def _binance_parse(m: dict, received: int) -> list[Any]:
    stream, d = m.get("stream", ""), m.get("data", m)
    symbol = d.get("s", stream.split("@")[0])
    if d.get("e") == "trade":
        return [trade("binance", symbol, d["p"], d["q"], d.get("T"), "SELL" if d.get("m") else "BUY", d.get("t"), received)]
    if d.get("e") == "depthUpdate":
        return [book("binance", symbol, d.get("b", []), d.get("a", []), d.get("E"), d.get("u"), received, BOOK_DEPTH)]
    return []


def _bybit_parse(m: dict, received: int) -> list[Any]:
    topic, d = m.get("topic", ""), m.get("data")
    if topic.startswith("publicTrade"):
        return [trade("bybit", x["s"], x["p"], x["v"], x.get("T"), x.get("S"), x.get("i"), received) for x in d]
    if topic.startswith("orderbook") and d:
        return [book("bybit", d["s"], d.get("b", []), d.get("a", []), m.get("ts"), d.get("u"), received, BOOK_DEPTH)]
    return []


def _kraken_parse(m: dict, received: int) -> list[Any]:
    channel, data = m.get("channel"), m.get("data", [])
    if channel == "trade":
        return [trade("kraken", x["symbol"], x["price"], x["qty"], int(float(x["timestamp"])*1000), x.get("side"), x.get("trade_id"), received) for x in data]
    if channel == "book" and data:
        x = data[0]
        return [book("kraken", x["symbol"], [(i["price"], i["qty"]) for i in x.get("bids", [])], [(i["price"], i["qty"]) for i in x.get("asks", [])], received, x.get("checksum"), received, BOOK_DEPTH)]
    return []


def _upbit_parse(m: dict, received: int) -> list[Any]:
    if m.get("type") == "trade":
        return [trade("upbit", m["code"], m["trade_price"], m["trade_volume"], m.get("trade_timestamp"), "BUY" if m.get("ask_bid") == "BID" else "SELL", m.get("sequential_id"), received)]
    if m.get("type") == "orderbook":
        units = m.get("orderbook_units", [])
        return [book("upbit", m["code"], [(x["bid_price"], x["bid_size"]) for x in units], [(x["ask_price"], x["ask_size"]) for x in units], m.get("timestamp"), None, received, BOOK_DEPTH)]
    return []


def _bithumb_parse(m: dict, received: int) -> list[Any]:
    if m.get("type") == "trade":
        return [trade("bithumb", m["code"], m["trade_price"], m["trade_volume"], m.get("trade_timestamp"), "BUY" if m.get("ask_bid") == "BID" else "SELL", m.get("sequential_id"), received)]
    if m.get("type") == "orderbook":
        units = m.get("orderbook_units", [])
        return [book("bithumb", m["code"], [(x["bid_price"], x["bid_size"]) for x in units], [(x["ask_price"], x["ask_size"]) for x in units], m.get("timestamp"), None, received, BOOK_DEPTH)]
    return []


def _coinone_parse(m: dict, received: int) -> list[Any]:
    d = m.get("data", m)
    quote, target = d.get("quote_currency", "KRW"), d.get("target_currency", "")
    symbol = f"{target}-{quote}"
    if m.get("channel") == "TRADE":
        items = d.get("trades", [d])
        return [trade("coinone", symbol, x["price"], x.get("qty", x.get("quantity")), x.get("timestamp", received), x.get("is_seller_maker") and "SELL" or "BUY", x.get("id"), received) for x in items]
    if m.get("channel") == "ORDERBOOK":
        return [book("coinone", symbol, [(x["price"], x.get("qty", x.get("quantity"))) for x in d.get("bids", [])], [(x["price"], x.get("qty", x.get("quantity"))) for x in d.get("asks", [])], d.get("timestamp", received), d.get("id"), received, BOOK_DEPTH)]
    return []


def _global_url(base: str):
    return lambda symbols: base


def _binance_url(symbols: list[str]) -> str:
    streams = "/".join(f"{s.lower()}usdt@trade/{s.lower()}usdt@depth10@100ms" for s in symbols)
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


def _bybit_sub(symbols: list[str]):
    return {"op":"subscribe", "args":[x for s in symbols for x in (f"publicTrade.{s}USDT", f"orderbook.50.{s}USDT")]}


def _kraken_sub(symbols: list[str]):
    pairs = [f"{s}/USD" for s in symbols]
    return [{"method":"subscribe","params":{"channel":"trade","symbol":pairs}}, {"method":"subscribe","params":{"channel":"book","depth":10,"snapshot":True,"symbol":pairs}}]


def _korea_sub(symbols: list[str]):
    codes = [f"KRW-{s}" for s in symbols]
    return [{"ticket":str(uuid.uuid4())},{"type":"trade","codes":codes},{"type":"orderbook","codes":codes},{"format":"DEFAULT"}]


def _coinone_sub(symbols: list[str]):
    return [{"request_type":"SUBSCRIBE","channel":ch,"topic":{"quote_currency":"KRW","target_currency":s}} for s in symbols for ch in ("TRADE", "ORDERBOOK")]


VENUE_ADAPTERS = {
    "binance": Adapter("binance", _binance_url, lambda _: None, _binance_parse),
    "bybit": Adapter("bybit", _global_url("wss://stream.bybit.com/v5/public/spot"), _bybit_sub, _bybit_parse),
    "kraken": Adapter("kraken", _global_url("wss://ws.kraken.com/v2"), _kraken_sub, _kraken_parse),
    "upbit": Adapter("upbit", _global_url("wss://api.upbit.com/websocket/v1"), _korea_sub, _upbit_parse),
    "bithumb": Adapter("bithumb", _global_url("wss://pubwss.bithumb.com/pub/ws"), _korea_sub, _bithumb_parse),
    "coinone": Adapter("coinone", _global_url("wss://stream.coinone.co.kr"), _coinone_sub, _coinone_parse),
}


class CollectorSupervisor:
    def __init__(self, symbols: list[str], sink: Callable[[Any], None]):
        self.symbols, self.sink = symbols, sink
        self.last_message: dict[str, int] = {}
        self.reconnects: dict[str, int] = {x: 0 for x in VENUE_ADAPTERS}

    async def run_venue(self, session: aiohttp.ClientSession, adapter: Adapter) -> None:
        attempt = 0
        while True:
            try:
                async with session.ws_connect(adapter.url(self.symbols), heartbeat=20, receive_timeout=STALE_FEED_SEC) as ws:
                    payloads = adapter.subscribe(self.symbols)
                    if payloads:
                        if not isinstance(payloads, list): payloads = [payloads]
                        for payload in payloads: await ws.send_json(payload)
                    attempt = 0
                    async for msg in ws:
                        if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY): continue
                        received = now_ms(); self.last_message[adapter.venue] = received
                        raw = msg.data.decode() if isinstance(msg.data, bytes) else msg.data
                        try: parsed = json.loads(raw)
                        except (ValueError, TypeError): continue
                        for event in adapter.parse(parsed, received): self.sink(event)
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.reconnects[adapter.venue] += 1
                delay = RECONNECT_BACKOFF_SEC[min(attempt, len(RECONNECT_BACKOFF_SEC)-1)]
                LOG.warning("venue=%s reconnect=%s delay=%s error=%r", adapter.venue, self.reconnects[adapter.venue], delay, exc)
                attempt += 1; await asyncio.sleep(delay)

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await asyncio.gather(*(self.run_venue(session, a) for a in VENUE_ADAPTERS.values()))

    def diagnostics(self) -> dict[str, Any]:
        current = now_ms()
        return {v: {"last_message_ms": self.last_message.get(v), "age_ms": current-self.last_message[v] if v in self.last_message else None, "stale": v not in self.last_message or current-self.last_message[v] > STALE_FEED_SEC*1000, "reconnects": self.reconnects[v]} for v in VENUE_ADAPTERS}
