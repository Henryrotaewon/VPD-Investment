"""Lossless migration, immutable audit evidence and bounded archive transfers."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi1.fast_observe import Observer, DAY, FIVE
from magi1.fast_candle_store import enable, compact, historical
from magi1.fast_reference import reference_state
from magi1.archive import Archiver, hour_end
from magi1.tests.test_archive import FakeDrive, ArchiveTests
from magi2.capture_audit import Evidence


class MigrationTests(unittest.TestCase):
    def test_bounded_restart_keeps_all_values_and_reference_exact(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'observe.db';t=20000*DAY+100*FIVE
            o=Observer(path,t);db=o.db;enable(db)
            full=t-FIVE
            values={full-k*DAY: float(k)/7 for k in range(1,11)}
            values[full-11*FIVE]=0.
            db.executemany('INSERT INTO baseline VALUES(?,?,?)',
                           [('KRW-X',key,value) for key,value in values.items()])
            db.execute('INSERT INTO recent5m VALUES(?,?,?,?,?,?,?,?,?)',
                       ('KRW-X',full,1,1,1,1,1,200,'API'))
            before=reference_state(db,'KRW-X',t)
            compact(db,t,batch_size=3);db.commit()
            self.assertEqual(db.execute('SELECT count(*) FROM baseline').fetchone()[0],8)
            self.assertEqual(reference_state(db,'KRW-X',t),before)
            db.close()
            o=Observer(path,t);db=o.db  # auto-detect packed data after restart
            while db.execute('SELECT count(*) FROM baseline').fetchone()[0]:
                compact(db,t,batch_size=3)
            self.assertEqual(historical(db,'KRW-X',list(values)),values)
            self.assertEqual(reference_state(db,'KRW-X',t),before)
            self.assertEqual(historical(db,'KRW-X',[full-12*DAY]),{})
            db.close()

    def test_failed_encoding_rolls_back_whole_batch_preserving_source(self):
        with tempfile.TemporaryDirectory() as root:
            o=Observer(Path(root)/'db',20*DAY);db=o.db;enable(db)
            db.executemany('INSERT INTO baseline VALUES(?,?,?)',
                           [('A',19*DAY,1.25),('B',19*DAY,2.5)])
            db.commit()
            import zlib
            real=zlib.compress;calls=[]
            def fail_second(value):
                calls.append(value)
                if len(calls)==2:raise RuntimeError('simulated failure')
                return real(value)
            with patch('magi1.fast_candle_store.zlib.compress',side_effect=fail_second):
                with self.assertRaises(RuntimeError):compact(db,20*DAY)
            self.assertEqual(db.execute('SELECT count(*) FROM baseline').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM turnover_days').fetchone()[0],0)
            db.close()

    def test_duplicate_evidence_never_recompresses_or_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            evidence=Evidence(root)
            evidence.add('fast','x',100,'KRW-X','CAPTURE',{'price':1.})
            with patch('magi2.capture_audit.zlib.compress',side_effect=AssertionError('duplicate compressed')):
                evidence.add('fast','x',100,'KRW-X','CAPTURE',{'price':2.})
            self.assertEqual(evidence.rows('fast','KRW-X',0,101)[0]['payload'],{'price':1.})
            evidence.db.close()


class ArchiveEfficiencyTests(ArchiveTests):
    def test_new_raw_and_restart_reuse_daily_snapshot(self):
        drive=FakeDrive();archiver=Archiver(self.storage)
        self.raw(2);asyncio.run(archiver.cycle(drive))
        self.raw(3);asyncio.run(Archiver(self.storage).cycle(drive))
        research=[r for r in drive.rows.values() if r['appProperties']['kind']=='research']
        self.assertEqual(len(research),1)
        self.assertEqual(len([r for r in drive.rows.values() if r['appProperties']['kind']=='raw']),2)

    def test_new_live_coverage_does_not_authorize_expiry_from_old_snapshot(self):
        drive=FakeDrive();archiver=Archiver(self.storage)
        asyncio.run(archiver.cycle(drive))  # no analysis coverage in this proof
        p=self.raw(10);end=hour_end(p)
        self.storage.checkpoint('analysis_coverage_v1',{'start':end-7200,'through':end+7200})
        with patch.dict('os.environ',{'MAGI1_ARCHIVE_DELETE_REMOTE':'1'}):
            asyncio.run(archiver.cycle(drive))
        self.assertFalse(drive.deleted)
        raw=[r for r in drive.rows.values() if r['appProperties']['kind']=='raw'][0]
        self.assertEqual(raw['appProperties']['analysis_verified'],'false')

    def test_permanent_deletion_defaults_off_even_with_coverage(self):
        p=self.raw(10);end=hour_end(p)
        self.storage.checkpoint('analysis_coverage_v1',{'start':end-7200,'through':end+7200})
        drive=FakeDrive()
        with patch.dict('os.environ',{'MAGI1_ARCHIVE_DELETE_REMOTE':'0'}):
            asyncio.run(Archiver(self.storage).cycle(drive))
        self.assertFalse(drive.deleted)


if __name__=='__main__':unittest.main()
