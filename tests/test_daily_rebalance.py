import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from magi2.daily_rebalance import DailyRebalance
from magi2 import server_runner as server

AT = datetime.fromisoformat('2026-09-28T07:20:00+09:00')
CONFIG = {'mode':'PAPER','daily_rebalance':{'enabled':True,'hour':7,'minute':20}}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.log=Mock();self.notify=Mock()
        self.schedule=DailyRebalance(self.root,CONFIG,self.log,self.notify)
        self.start=Mock(return_value=True)

    def tearDown(self):
        self.schedule.db.close();self.tmp.cleanup()

    def test_exact_kst_minute_fixed_cutoff_and_once_across_restart(self):
        self.assertFalse(self.schedule.tick(AT-timedelta(seconds=1),self.start))
        self.assertTrue(self.schedule.tick((AT+timedelta(seconds=7)).astimezone(timezone.utc),self.start))
        self.start.assert_called_once_with('scheduled-vpd-2026-09-28',AT)
        self.schedule.db.close();self.schedule=DailyRebalance(self.root,CONFIG,self.log,self.notify)
        self.assertFalse(self.schedule.tick(AT+timedelta(seconds=10),self.start))
        self.assertEqual(self.schedule.next_run(AT),AT+timedelta(days=1))

    def test_busy_and_exception_never_retry_at_0730(self):
        self.start.return_value=False
        self.assertFalse(self.schedule.tick(AT,self.start))
        self.assertFalse(self.schedule.tick(AT+timedelta(seconds=20),self.start))
        self.assertFalse(self.schedule.tick(AT+timedelta(minutes=10),self.start))
        self.start.assert_called_once();self.notify.assert_called_once()
        self.start.side_effect=RuntimeError('failure')
        self.assertFalse(self.schedule.tick(AT+timedelta(days=1),self.start))
        self.assertFalse(self.schedule.tick(AT+timedelta(days=1,seconds=10),self.start))
        self.assertEqual(self.start.call_count,2)

    def test_crash_after_claim_and_late_start_do_not_replay(self):
        with self.schedule.db:
            self.schedule.db.execute('INSERT INTO claims VALUES(?,?,?)',('2026-09-28','scheduled-vpd-2026-09-28','claimed'))
        self.assertFalse(self.schedule.tick(AT,self.start))
        self.assertFalse(self.schedule.tick(AT+timedelta(days=1,minutes=10),self.start))
        self.assertEqual(self.schedule.next_run(AT+timedelta(days=1,minutes=10)),AT+timedelta(days=2))
        self.start.assert_not_called()
        self.assertTrue(self.schedule.tick(AT+timedelta(days=2),self.start))

    def test_live_or_disabled_config_never_dispatches(self):
        for cfg in ({'mode':'LIVE',**{'daily_rebalance':CONFIG['daily_rebalance']}}, {'mode':'PAPER'}):
            s=DailyRebalance(self.root,cfg,self.log,self.notify)
            self.assertFalse(s.tick(AT,self.start));s.db.close()
        self.start.assert_not_called()

    def test_scheduled_and_manual_use_same_scan_entrypoint(self):
        with patch.object(server,'ENGINE_JOB',None),patch.object(server,'SCAN_JOB',None),patch.object(server,'SCAN_CONTEXT',None),patch.object(server,'SCAN_EXECUTOR') as worker,patch.object(server,'telegram'),patch.object(server,'STATE_DIR',self.root):
            self.schedule.tick(AT+timedelta(seconds=12),lambda ident,at:server.start_engine('morning',request_id=ident,scan_at=at))
            worker.submit.assert_called_once_with(server.build_vpd,self.root/'vpd_rebalance.json',AT)
            self.assertEqual(server.SCAN_CONTEXT['asof'],AT.isoformat())

    def test_morning_view_reads_scheduled_snapshot_without_github_call(self):
        (self.root/'vpd_scheduled_snapshot.json').write_text(json.dumps({'asof':AT.isoformat(),'top10':[]}))
        with patch.object(server,'STATE_DIR',self.root),patch.object(server.requests,'get') as get,patch.object(server,'telegram') as send:
            server.return_magi1_state('morning')
            get.assert_not_called()
            self.assertIn(AT.isoformat(),send.call_args.args[0])
