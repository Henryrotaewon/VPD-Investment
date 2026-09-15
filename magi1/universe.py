from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiohttp

from .config import UNIVERSE_SIZE


ENDPOINTS = {
    "binance": "https://api.binance.com/api/v3/ticker/24hr",
    "bybit": "https://api.bybit.com/v5/market/tickers?category=spot",
    "kraken": "https://api.kraken.com/0/public/Ticker",
    "upbit": "https://api.upbit.com/v1/ticker/all?quote_currencies=KRW",
    "bithumb": "https://api.bithumb.com/v1/ticker/all?quote_currencies=KRW",
    "coinone": "https://api.coinone.co.kr/public/v2/ticker_new/KRW?additional_data=true",
}


def parse_markets(venue: str, payload: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if venue == "binance":
        for x in payload:
            if x["symbol"].endswith("USDT"): out[x["symbol"][:-4]] = float(x.get("quoteVolume", 0))
    elif venue == "bybit":
        for x in payload["result"]["list"]:
            if x["symbol"].endswith("USDT"): out[x["symbol"][:-4]] = float(x.get("turnover24h", 0))
    elif venue == "kraken":
        for pair, x in payload["result"].items():
            if pair.endswith(("USD", "USDT")):
                base = pair.removeprefix("X").split("USD")[0].replace("XBT", "BTC")
                out[base] = float(x["v"][1]) * float(x["c"][0])
    elif venue in {"upbit", "bithumb"}:
        for x in payload:
            market = x["market"]
            if market.startswith("KRW-"): out[market[4:]] = float(x.get("acc_trade_price_24h", x.get("acc_trade_price", 0)))
    elif venue == "coinone":
        for x in payload.get("tickers", []): out[x["target_currency"].upper()] = float(x.get("quote_volume", 0))
    return out


async def discover(path: str | None = None, size: int = UNIVERSE_SIZE) -> dict[str, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        async def get(v: str, u: str):
            async with session.get(u) as r: r.raise_for_status(); return v, parse_markets(v, await r.json())
        markets = dict(await asyncio.gather(*(get(*x) for x in ENDPOINTS.items())))
    common = set.intersection(*(set(x) for x in markets.values()))
    # Percentile-like venue rank aggregation avoids comparing KRW and USD notionals.
    ranks = {v: {a: i / max(1, len(m)-1) for i, (a, _) in enumerate(sorted(m.items(), key=lambda x:x[1], reverse=True))} for v,m in markets.items()}
    scored = sorted(((sum(1-ranks[v][a] for v in markets)/len(markets), a) for a in common), reverse=True)
    result = {"selected_at_ms": time.time_ns()//1_000_000, "method":"six_venue_common_mean_liquidity_percentile", "assets":[a for _,a in scored[:size]], "scores":{a:s for s,a in scored[:size]}, "eligible_count":len(common)}
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
