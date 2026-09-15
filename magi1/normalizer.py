from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from .schema import BookEvent, BookLevel, TradeEvent


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def split_symbol(symbol: str) -> tuple[str, str]:
    value = symbol.upper().replace("/", "-").replace("_", "-")
    parts = value.split("-")
    if len(parts) == 2:
        if parts[0] in {"KRW", "USD", "USDT"}:
            return parts[1], parts[0]
        return parts[0], parts[1]
    for quote in ("USDT", "KRW", "USD"):
        if value.endswith(quote):
            return value[: -len(quote)], quote
    raise ValueError(f"unsupported symbol: {symbol}")


def trade(venue: str, symbol: str, price: Any, quantity: Any,
          exchange_ts_ms: Optional[Any], side: Optional[str] = None,
          trade_id: Optional[Any] = None, received_ts_ms: Optional[int] = None) -> TradeEvent:
    base, quote = split_symbol(symbol)
    return TradeEvent(venue, f"{base}-{quote}", base, quote,
                      int(exchange_ts_ms) if exchange_ts_ms is not None else None,
                      received_ts_ms if received_ts_ms is not None else now_ms(), float(price), float(quantity),
                      side.upper() if side else None,
                      str(trade_id) if trade_id is not None else None)


def book(venue: str, symbol: str, bids: Iterable[Iterable[Any]], asks: Iterable[Iterable[Any]],
         exchange_ts_ms: Optional[Any], sequence: Optional[Any] = None,
         received_ts_ms: Optional[int] = None, depth: int = 10) -> BookEvent:
    base, quote = split_symbol(symbol)
    levels = lambda rows: tuple(BookLevel(float(x[0]), float(x[1])) for x in list(rows)[:depth])
    return BookEvent(venue, f"{base}-{quote}", base, quote,
                     int(exchange_ts_ms) if exchange_ts_ms is not None else None,
                     received_ts_ms if received_ts_ms is not None else now_ms(), levels(sorted(bids, key=lambda x: float(x[0]), reverse=True)), levels(sorted(asks, key=lambda x: float(x[0]))),
                     str(sequence) if sequence is not None else None)
