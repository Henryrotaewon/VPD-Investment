import asyncio
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from magi2.fast_lab import live_job as job


class LiveJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_byte_limit_cancels_stream_and_closes_tape(self):
        async def discover(*args):
            return {'kind':'universe','venue':'upbit','selected_count':1,'eligible_count':1,
                    'markets':[{'symbol':'KRW-BTC'}]}
        cancelled=[]
        async def stream(session,venue,markets,write,connection):
            try:
                for i in range(100):write({'kind':'trade','symbol':'KRW-BTC','received_ts_ms':i,'event_ts_ms':i-5,'padding':'x'*300})
                await asyncio.Future()
            finally:cancelled.append(True)
        with tempfile.TemporaryDirectory() as directory, patch.object(job,'discover',discover), patch.object(job,'stream',stream):
            path=Path(directory)/'tape.gz'
            quality=await job.capture_venue('upbit',path,5,1500)
            with gzip.open(path,'rt') as source:rows=[json.loads(line) for line in source]
            self.assertEqual(quality['stop_reason'],'BYTE_LIMIT')
            self.assertEqual(rows[-1]['kind'],'capture_end')
            self.assertTrue(cancelled)
            self.assertLess(quality['uncompressed_bytes'],2000)

    async def test_discovery_failure_still_has_finalized_tape(self):
        async def fail(*args):raise TimeoutError()
        with tempfile.TemporaryDirectory() as directory, patch.object(job,'discover',fail):
            path=Path(directory)/'tape.gz'
            quality=await job.capture_venue('upbit',path,1,10000)
            self.assertEqual(quality['counts']['gap'],1)
            self.assertEqual(quality['counts']['capture_end'],1)
            self.assertEqual(quality['stop_reason'],'CAPTURE_ERROR_TimeoutError')

    async def test_claim_prevents_repeated_capture(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(job.os.environ,{},clear=True), patch.object(job,'capture_venue') as capture:
            (Path(directory)/'run1').mkdir()
            await job.run(SimpleNamespace(output_dir=directory,run_id='run1'))
            capture.assert_not_called()

    def test_quality_distinguishes_missing_clock_and_no_activity(self):
        quality=job.Quality()
        quality.observe({'kind':'universe','markets':[{'symbol':'A'},{'symbol':'B'}]})
        quality.observe({'kind':'trade','symbol':'A','received_ts_ms':100,'event_ts_ms':90})
        quality.observe({'kind':'book','symbol':'A','received_ts_ms':200})
        summary=quality.summary()
        self.assertEqual(summary['markets_with_trades'],1)
        self.assertEqual(summary['no_trade_markets'],['B'])
        self.assertEqual(summary['missing_event_timestamp'],{'book':1})
        self.assertEqual(summary['receipt_minus_exchange_clock']['trade']['p95_bucket_ms'],10)
