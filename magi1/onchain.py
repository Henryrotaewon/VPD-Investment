from __future__ import annotations

import abc
import json
import logging
import time
from typing import Any, AsyncIterator

from .schema import ShockOriginCandidate

import aiohttp


class OnChainProvider(abc.ABC):
    name="base"
    @abc.abstractmethod
    async def events(self) -> AsyncIterator[dict[str,Any]]: ...


class RawPublicProvider(OnChainProvider):
    """Adapter boundary for public node/explorer feeds. URLs are supplied by env."""
    name="public-raw"
    def __init__(self, source: AsyncIterator[dict[str,Any]]): self.source=source
    async def events(self):
        async for x in self.source: yield x


class BitcoinPublicWebSocketProvider(OnChainProvider):
    """Public raw BTC transaction feed; address labels are intentionally unknown.

    The first implementation kept only the transaction hash and output sum.  That
    is insufficient for a later exchange/wallet-flow graph, so we retain the
    address-level transaction shape as *raw evidence*.  No ownership or direction
    is inferred here; enrichment is a separate, point-in-time step.
    """
    name="blockchain-info-public-ws"
    def __init__(self, minimum_btc: float = 100.0): self.minimum_sats=int(minimum_btc*100_000_000)

    @staticmethod
    def normalize_transaction(x: dict[str, Any]) -> dict[str, Any]:
        inputs=[]
        for item in x.get("inputs", []):
            prev=item.get("prev_out") or {}
            inputs.append({"address": prev.get("addr"), "value_sats": int(prev.get("value", 0) or 0)})
        outputs=[]
        for item in x.get("out", []):
            outputs.append({"address": item.get("addr"), "value_sats": int(item.get("value", 0) or 0), "spent": item.get("spent")})
        input_total=sum(i["value_sats"] for i in inputs)
        output_total=sum(o["value_sats"] for o in outputs)
        return {
            "tx_hash": x.get("hash"),
            "inputs": inputs,
            "outputs": outputs,
            "input_total_sats": input_total,
            "output_total_sats": output_total,
            "fee_sats": input_total-output_total if input_total >= output_total else None,
            "confirmation_status": "UNCONFIRMED",
            "block_height": None,
            "raw_provider": "blockchain-info-public-ws",
        }

    async def events(self):
        backoff=1
        while True:
            try:
                async with aiohttp.ClientSession() as session, session.ws_connect("wss://ws.blockchain.info/inv",heartbeat=20) as ws:
                    await ws.send_json({"op":"unconfirmed_sub"}); backoff=1
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT: continue
                        x=json.loads(msg.data).get("x",{}); tx=self.normalize_transaction(x); amount=tx["output_total_sats"]
                        if amount>=self.minimum_sats:
                            yield {"asset":"BTC","direction":"UNKNOWN","event_ts_ms":int(x.get("time",time.time())*1000),"category":"large_transfer","amount":amount/100_000_000,"tx_hash":x.get("hash"),"confidence":None,"metadata":{"label":"unclassified_public_raw", "transaction":tx}}
            except Exception as exc:
                logging.getLogger(__name__).warning("onchain reconnect error=%r",exc)
                await __import__("asyncio").sleep(backoff); backoff=min(backoff*2,30)


class OnChainShockAdapter:
    def __init__(self, provider: OnChainProvider): self.provider=provider
    async def events(self):
        async for x in self.provider.events():
            now=time.time_ns()//1_000_000
            yield ShockOriginCandidate("onchain",self.provider.name,x["asset"],x.get("direction","UNKNOWN"),int(x.get("event_ts_ms",now)),now,x["category"],x.get("amount"),x.get("tx_hash"),x.get("confidence"),x.get("metadata",{}),False)


class EnrichmentProvider(abc.ABC):
    """Replaceable Arkham/Nansen/Glassnode-compatible enrichment interface."""
    @abc.abstractmethod
    async def enrich(self, candidate: ShockOriginCandidate) -> ShockOriginCandidate: ...
