from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from magi2.vpd_schedule import claim, due, KST
from magi2 import server_runner as server

T = datetime(2026,10,10,7,30,tzinfo=KST)


class ScheduleTests(unittest.TestCase):
    def test_kst_window_boundary_and_no_old_catchup(self):
        self.assertIsNone(due(T-timedelta(seconds=1)))
        self.assertEqual(due(T.astimezone(timezone.utc)),T)
        self.assertEqual(due(T+timedelta(minutes=14,seconds=59)),T)
        self.assertIsNone(due(T+timedelta(minutes=15)))
        self.assertIsNone(due(T.replace(hour=19)))
        with self.assertRaises(ValueError): due(T.replace(tzinfo=None))

    def test_durable_claim_unique_across_workers_restart_and_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda _: claim(tmp,T), range(4)))
            self.assertEqual(sum(v is not None for v in values),1)
            self.assertIsNone(claim(tmp,T+timedelta(minutes=1)))
            self.assertEqual(claim(tmp,T+timedelta(days=1)),'vpd-daily-0730-2026-10-11')

    def test_busy_deferred_and_claimed_failure_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'STATE_DIR',Path(tmp)), \
             patch.object(server,'SCAN_JOB',None),patch.object(server,'ENGINE_JOB',None), \
             patch.object(server,'start_engine',return_value=True) as start,patch.object(server,'telegram'):
            with patch.object(server,'ENGINE_JOB',Mock(done=lambda:False)):
                server.consume_scheduled_rebalance(T)
            start.assert_not_called()
            server.consume_scheduled_rebalance(T+timedelta(seconds=1))
            start.assert_called_once_with('morning',request_id='vpd-daily-0730-2026-10-10')
            server.consume_scheduled_rebalance(T+timedelta(minutes=1))
            start.assert_called_once()
            start.side_effect=RuntimeError('fail')
            with self.assertRaises(RuntimeError):server.consume_scheduled_rebalance(T+timedelta(days=1))
            server.consume_scheduled_rebalance(T+timedelta(days=1,minutes=1))
            self.assertEqual(start.call_count,2)

    def test_non_paper_never_claims_or_dispatches(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'STATE_DIR',Path(tmp)), \
             patch.object(server,'SCAN_JOB',None),patch.object(server,'ENGINE_JOB',None), \
             patch.object(server,'load_json',return_value={'mode':'LIVE'}),patch.object(server,'start_engine') as start:
            server.consume_scheduled_rebalance(T)
            start.assert_not_called()
            self.assertFalse(list(Path(tmp).iterdir()))
