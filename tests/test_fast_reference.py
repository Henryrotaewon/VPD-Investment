import tempfile
import unittest
from pathlib import Path

from magi1.fast_observe import Observer, DAY, FIVE
from magi1.fast_reference import bounds, reference_state
from magi1.fast_bootstrap import initialize, prune, put
from magi1.fast_repair import Repair


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.day = 30 * DAY  # 09:00 KST
        self.path = str(Path(self.tmp.name) / 'observe.db')
        self.o = Observer(self.path, self.day)
        initialize(self.o.db)
        self.o.connected('a', ['KRW-X'], self.day - DAY)

    def tearDown(self):
        self.o.db.close()
        self.tmp.cleanup()

    def seed(self, end, symbol='KRW-X', current=300, past=100):
        full = end // FIVE * FIVE - FIVE
        self.o.db.execute('INSERT OR REPLACE INTO recent5m VALUES(?,?,100,100,100,100,3,?,?)',
                          (symbol, full, current, 'API'))
        for k in range(1, 11):
            self.o.db.execute('INSERT OR REPLACE INTO baseline VALUES(?,?,?)',
                              (symbol, full-k*DAY, past))
        self.o.db.commit()
        return full

    def warm(self, end):
        self.o.current = end
        self.o.bars['KRW-X'].clear()
        for i in range(6):
            self.o.bars['KRW-X'].append(dict(t=end-60000+i*10000,value=1,buy=1,count=1,ofi=1))

    def window(self, amount, fresh=True):
        self.o.bar('KRW-X').update(value=amount,buy=amount,count=20,ofi=1)
        end = self.o.current + 10000
        stamp = end-1 if fresh else end-3000
        self.o.books['KRW-X'] = dict(ts=stamp,bp=99,ap=101)
        self.o.last_price['KRW-X'] = (stamp,100,stamp)
        self.o.advance(end)

    def test_midnight_prune_preserves_0855_tenth_day_and_capture(self):
        full = self.seed(self.day)
        # Exercise both pruning paths at the actual day boundary.
        self.o.evaluate(self.day)
        prune(self.o.db, self.day)
        self.assertIsNotNone(self.o.db.execute('SELECT value FROM baseline WHERE bucket=?',
                                              (full-10*DAY,)).fetchone())
        self.warm(self.day)
        self.window(30); self.window(90)
        self.assertEqual(self.o.report()['captures'], 1)

    def test_all_288_boundaries_retain_exact_tenth_day(self):
        for offset in range(0, DAY, FIVE):
            end = self.day + offset
            self.seed(end)
            prune(self.o.db, end)
            self.assertTrue(reference_state(self.o.db, 'KRW-X', end)['ready'], offset)
            self.assertEqual(bounds(end)[0], end-FIVE-10*DAY)

    def test_current_bar_and_history_are_independent(self):
        full = self.seed(self.day, current=600)
        self.o.db.execute('INSERT INTO baseline VALUES(?,?,?)', ('KRW-X', full, 99999))
        self.assertEqual(reference_state(self.o.db, 'KRW-X', self.day)['relative_value'], 6)
        self.o.db.execute('DELETE FROM recent5m')
        self.assertEqual(reference_state(self.o.db, 'KRW-X', self.day)['reasons'], ['CURRENT_BAR_MISSING'])

    def test_stale_repair_gate_clears_on_decision_without_worker(self):
        self.seed(self.day)
        self.o.repair_blocked.add('KRW-X')
        self.o.previous['KRW-X'] = (self.day, True)
        self.warm(self.day)
        self.window(30)
        self.assertNotIn('KRW-X', self.o.repair_blocked)
        self.assertEqual(self.o.report()['captures'], 0)  # fresh confirmation still required
        self.window(90)
        self.assertEqual(self.o.report()['captures'], 1)

    def test_arbitrary_five_minute_rollover_clears_stale_gate(self):
        end = self.day + 7*FIVE
        self.seed(end)
        self.o.repair_blocked.add('KRW-X')
        self.warm(end)
        self.window(30); self.window(90)
        self.assertEqual(self.o.report()['captures'], 1)

    def test_current_completion_unblocks_without_next_repair_cycle(self):
        full = self.seed(self.day)
        self.o.db.execute('DELETE FROM recent5m')
        self.assertFalse(self.o.refresh_reference('KRW-X', self.day)['ready'])
        put(self.o.db, 'KRW-X', [full,100,100,100,100,3,300], self.day, 'API')
        self.warm(self.day)
        self.window(30); self.window(90)
        self.assertEqual(self.o.report()['captures'], 1)

    def test_missing_one_history_day_never_shortens_denominator(self):
        full = self.seed(self.day)
        self.o.db.execute('DELETE FROM baseline WHERE bucket=?', (full-5*DAY,))
        self.warm(self.day)
        self.window(30); self.window(90)
        self.assertEqual(self.o.report()['captures'], 0)
        ref = reference_state(self.o.db, 'KRW-X', self.day)
        self.assertEqual(ref['baseline_days'], 9)
        self.assertIsNone(ref['relative_value'])
        self.assertEqual(ref['reasons'], ['HISTORY_INCOMPLETE'])

    def test_current_partial_and_future_history_never_substitute(self):
        self.seed(self.day+FIVE)
        ref = reference_state(self.o.db, 'KRW-X', self.day)
        self.assertFalse(ref['ready'])
        self.assertIn('CURRENT_BAR_MISSING', ref['reasons'])

    def test_zero_history_blocks_but_proven_zero_current_does_not(self):
        self.seed(self.day, past=0)
        self.assertEqual(reference_state(self.o.db, 'KRW-X', self.day)['reasons'], ['HISTORY_ZERO_VALUE'])
        self.seed(self.day, current=0)
        ref = reference_state(self.o.db, 'KRW-X', self.day)
        self.assertTrue(ref['ready']); self.assertEqual(ref['relative_value'], 0)

    def test_stale_feed_still_blocks_with_complete_reference(self):
        self.seed(self.day)
        self.warm(self.day)
        self.window(30, fresh=False); self.window(90, fresh=False)
        self.assertEqual(self.o.report()['captures'], 0)
        self.assertEqual(self.o.report()['decision_reasons']['STALE_FEED'], 1)

    def test_reconnect_requires_live_warmup_but_keeps_reference(self):
        self.seed(self.day)
        self.o.gap('a', self.day, 'TEST')
        self.o.connected('a', ['KRW-X'], self.day)
        self.window(30); self.window(90)
        self.assertTrue(reference_state(self.o.db, 'KRW-X', self.day)['ready'])
        self.assertEqual(self.o.report()['captures'], 0)
        self.assertEqual(self.o.report()['decision_reasons']['LIVE_WARMUP'], 1)

    def test_unrelated_missing_history_does_not_block_healthy_market(self):
        self.seed(self.day)
        self.o.connected('b', ['KRW-NEW'], self.day-DAY)
        Repair(self.o, ['KRW-X','KRW-NEW'], self.path, clock=lambda:self.day).gate('KRW-NEW')
        self.warm(self.day)
        self.window(30); self.window(90)
        self.assertEqual(self.o.report()['captures'], 1)
        self.assertIn('KRW-NEW', self.o.repair_blocked)
        self.assertNotIn('KRW-X', self.o.repair_blocked)


if __name__ == '__main__':
    unittest.main()
