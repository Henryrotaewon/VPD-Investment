from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collectors import CollectorSupervisor
from .config import DEFAULT_DATA_DIR
from .features import FeatureEngine
from .formation import FormationEngine
from .onchain import BitcoinPublicWebSocketProvider, OnChainShockAdapter
from .report import build_daily
from .storage import Storage
from .universe import discover

LOG=logging.getLogger("magi1")
KST=timezone(timedelta(hours=9))


class App:
    def __init__(self, root: str, assets: list[str]):
        self.storage=Storage(root); self.features=FeatureEngine(); self.formation=FormationEngine(self.storage.append)
        self.collector=CollectorSupervisor(assets,self.ingest)

    def ingest(self,event):
        kind="trade" if event.__class__.__name__=="TradeEvent" else "book"
        self.storage.append_raw(kind,event)
        for feature in self.features.ingest(event): self.formation.ingest(feature)

    async def report_loop(self):
        last=None
        while True:
            now=datetime.now(KST)
            if now.hour==7 and now.date()!=last:
                path=build_daily(self.storage,now); LOG.info("daily_report=%s",path); last=now.date()
            await asyncio.sleep(30)

    async def diagnostics_loop(self):
        while True:
            await asyncio.sleep(30); LOG.info("feed_diagnostics=%s storage=%s",json.dumps(self.collector.diagnostics()),self.storage.health())

    async def onchain_loop(self):
        minimum=float(os.getenv("MAGI1_ONCHAIN_MIN_BTC","100"))
        async for event in OnChainShockAdapter(BitcoinPublicWebSocketProvider(minimum)).events():
            self.storage.append_raw("onchain",event); self.storage.append("onchain_candidate",event)

    async def run(self):
        await asyncio.gather(self.collector.run(),self.onchain_loop(),self.report_loop(),self.diagnostics_loop())


async def main_async(args):
    mode=os.getenv("MAGI1_MODE","COLLECT_ONLY")
    if mode!="COLLECT_ONLY": raise SystemExit("MAGI1_MODE must remain COLLECT_ONLY; order execution is not implemented")
    root=args.data_dir or os.getenv("MAGI1_DATA_DIR",DEFAULT_DATA_DIR)
    universe_path=str(Path(root)/"universe.json")
    if args.assets: assets=[x.strip().upper() for x in args.assets.split(",")]
    else: assets=(await discover(universe_path))["assets"]
    if not assets: raise SystemExit("common universe is empty")
    LOG.info("MAGI1 mode=%s assets=%s data=%s",mode,assets,root)
    await App(root,assets).run()


def main():
    p=argparse.ArgumentParser(); p.add_argument("--data-dir"); p.add_argument("--assets",help="diagnostic override; production defaults to automatic universe")
    args=p.parse_args(); logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main_async(args))


if __name__=="__main__": main()
