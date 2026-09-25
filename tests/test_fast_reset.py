import tempfile
import unittest
from pathlib import Path

from magi2.fast_reset import RESET_ID, reset_once
from magi2.fast_rank_paper import RankLedger


class FastResetTests(unittest.TestCase):
    def test_exact_scope_and_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = ('paper_state.json', 'paper_events.jsonl', 'vpd_rebalance.json',
                         'wave.sqlite3', 'fast_other.sqlite3')
            targets = [name + suffix for name in ('fast_paper.sqlite3', 'fast_evidence.sqlite3')
                       for suffix in ('', '-wal', '-shm')]
            for name in (*protected, *targets):
                (root / name).write_text('sentinel')
            logs = []
            self.assertTrue(reset_once(root, logs.append))
            for name in targets:
                self.assertFalse((root / name).exists())
            for name in protected:
                self.assertEqual((root / name).read_text(), 'sentinel')
            (root / 'fast_paper.sqlite3').write_text('new-cohort')
            self.assertFalse(reset_once(root, logs.append))
            self.assertEqual((root / 'fast_paper.sqlite3').read_text(), 'new-cohort')
            self.assertIn(RESET_ID, logs[0])

    def test_empty_ledger_stays_paused_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reset_once(root, lambda _: None)
            ledger = RankLedger(root / 'fast_paper.sqlite3', 1000)
            ledger.pause_and_clear(1000)
            self.assertIsNone(ledger.launch())
            self.assertFalse(ledger.activate_due(2000))
            ledger.db.close()
            self.assertFalse(reset_once(root, lambda _: None))
            ledger = RankLedger(root / 'fast_paper.sqlite3', 2000)
            self.assertFalse(ledger.control()['enabled'])
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0], 0)
            ledger.db.close()


if __name__ == '__main__':
    unittest.main()
