import copy
import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi2 import paper_engine as pe, refill_engine as refill, server_runner as server
from magi2.point_in_time_scan import SCHEMA

NOW = datetime.fromisoformat('2026-09-26T17:40:30+09:00')
ASOF = NOW.replace(second=0)


def row(coin, score=80, rank=1):
    return dict(coin=coin, market='KRW-'+coin, VPD=score, Rank=rank)


class RefillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = dict(cash_krw=600000., initial_cash_krw=3000000., positions={},
                          daily_equal_buy_date='2026-09-26', daily_equal_buy_krw=300000.)
        self.snap = dict(schema=SCHEMA, basis='POINT_IN_TIME', asof=ASOF.isoformat(),
                         generated_at=ASOF.isoformat(), top10=[row('X')], all_rows={'X':row('X')})
        self.patches = [patch.object(pe,'STATE_PATH',root/'paper_state.json'),
                        patch.object(pe,'EVENTS_PATH',root/'events.jsonl'),
                        patch.object(pe,'now_dt',return_value=NOW),
                        patch.object(pe,'load_morning_snapshot',side_effect=lambda *a:(self.snap,ASOF)),
                        patch.object(pe,'load_today_snapshot',side_effect=AssertionError('No GitHub dependency')),
                        patch.object(pe,'get_prices',return_value={'KRW-X':100}),
                        patch.object(pe,'telegram'), patch.object(pe,'send_current_status'),
                        patch.object(refill,'MIN_VPD',75)]
        values = [p.start() for p in self.patches]
        self.cache,self.remote,self.prices,self.send = values[3:7]
        pe.save_state(self.state)
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.tmp.cleanup)

    def test_refill_ignores_stale_github_and_repeat_cannot_double_buy(self):
        self.assertTrue(refill.refill(expected_asof=ASOF.isoformat()))
        state = pe.load_state()
        self.assertEqual(state['cash_krw'],300000)
        self.assertEqual(set(state['positions']),{'X'})
        self.assertEqual(state['positions']['X']['entry_session'],'REFILL')
        self.assertFalse(refill.refill(expected_asof=ASOF.isoformat()))
        self.assertEqual(pe.load_state()['cash_krw'],300000)
        self.remote.assert_not_called()

    def test_snapshot_failure_never_changes_account(self):
        self.cache.side_effect=None;self.cache.return_value=None
        before=pe.STATE_PATH.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'REFILL_SNAPSHOT_NOT_READY'):
            refill.refill(expected_asof=ASOF.isoformat())
        self.assertEqual(pe.STATE_PATH.read_bytes(),before)
        self.prices.assert_not_called()

    def test_expiry_after_price_request_never_buys(self):
        self.cache.side_effect=[(self.snap,ASOF),None]
        before=pe.STATE_PATH.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'REFILL_SNAPSHOT_EXPIRED'):
            refill.refill(expected_asof=ASOF.isoformat())
        self.assertEqual(pe.STATE_PATH.read_bytes(),before)
        self.assertFalse(pe.EVENTS_PATH.exists())

    def test_threshold_cooldown_and_same_snapshot_fallback_preserved(self):
        closed=dict(status='CLOSED',exit_reason='HARD_STOP',exit_at=NOW.isoformat())
        st=dict(positions={'X':closed},closed_positions=[dict(closed,coin='Y')])
        blocked=refill.same_day_cooldown(st,'2026-09-26')
        snap=copy.deepcopy(self.snap)
        snap['all_rows']={'X':row('X'),'Y':row('Y'),'LOW':row('LOW',74),'GOOD':row('GOOD',76)}
        rows,source=refill.candidate_rows(snap,{},blocked)
        self.assertEqual([r['coin'] for r in rows],['GOOD'])
        self.assertIn('차순위',source)

    def test_no_candidate_or_insufficient_cash_has_clear_reason(self):
        self.snap['top10']=[row('X',74)];self.snap['all_rows']={'X':row('X',74)}
        self.assertFalse(refill.refill(expected_asof=ASOF.isoformat()))
        self.assertIn('75+ 신규 후보가 없습니다',self.send.call_args.args[0])
        self.state['cash_krw']=299999;pe.save_state(self.state)
        self.assertFalse(refill.refill(expected_asof=ASOF.isoformat()))
        self.assertIn('예수금이 부족',self.send.call_args.args[0])

    def test_standalone_scans_without_github_token(self):
        with patch.object(pe,'build_snapshot',return_value=self.snap) as build:
            self.assertTrue(refill.refill())
            build.assert_called_once()
        self.remote.assert_not_called()

    def test_oneshot_refill_is_claimed_once_and_cannot_invoke_rebuild(self):
        request=dict(id='user_refill_fix',mode='refill',expires_at='2026-09-26T18:10:00+09:00')
        with patch.dict(os.environ,{'MAGI2_REBALANCE_ONCE':json.dumps(request)}), \
             patch.object(server,'STATE_DIR',Path(self.tmp.name)), \
             patch.object(server,'datetime') as clock,patch.object(server,'start_engine',return_value=True) as start:
            clock.now.return_value=NOW;clock.fromisoformat.side_effect=datetime.fromisoformat
            server.consume_startup_rebalance();server.consume_startup_rebalance()
            start.assert_called_once_with('refill',request_id='user_refill_fix')
            request.update(id='bad_mode',mode='rebuild')
            with patch.dict(os.environ,{'MAGI2_REBALANCE_ONCE':json.dumps(request)}):
                server.consume_startup_rebalance()
            self.assertEqual(start.call_count,1)


if __name__=='__main__': unittest.main()
