import asyncio
import hashlib
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from magi1.archive import Archiver, hour_end, backup
from magi1.storage import Storage

class FakeDrive:
    def __init__(self, fail=False):
        self.rows = {}; self.fail = fail; self.deleted = []
    async def folder(self): return 'folder'
    async def upload(self, path, folder, props):
        if self.fail: raise RuntimeError('Drive simulated upload failure')
        content = path.read_bytes(); ident = str(len(self.rows) + 1)
        row = {'id': ident, 'name':path.name, 'md5Checksum': hashlib.md5(content).hexdigest(), 'size': str(len(content)), 'appProperties': props}
        self.rows[ident] = row
        return row
    async def request(self, method, url, **kw):
        ident = url.rsplit('/',1)[-1]
        if method == 'DELETE': self.deleted.append(ident); return {}, {}
        return self.rows[ident], {}
    async def list(self, query):
        return [r for r in self.rows.values() if r['appProperties'].get('kind') == 'raw']

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.storage = Storage(self.tmp.name)
    def tearDown(self):
        self.storage.close(); self.tmp.cleanup()
    def raw(self, days=2):
        t = time.time() - days*86400
        name = 'trade-' + datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d-%H') + '.jsonl.gz'
        p = self.storage.raw/name; p.write_bytes(b'test-raw')
        import os
        os.utime(p, (t,t)); return p
    def test_filename_and_age_only_never_deletes(self):
        p = self.raw(10)
        self.assertIsNotNone(hour_end(p))
        self.storage.maintain(2)
        self.assertTrue(p.exists())
    def test_upload_failure_retains_raw_and_research(self):
        p=self.raw(); self.storage.append('test', {'event_ts_ms':1});self.storage.flush()
        with self.assertRaises(RuntimeError): asyncio.run(Archiver(self.storage).cycle(FakeDrive(True)))
        self.assertTrue(p.exists()); self.assertEqual(len(self.storage.query('test',0,2)),1)
    def test_verified_upload_moves_local_but_holds_legacy_remote(self):
        p=self.raw(10); drive=FakeDrive()
        asyncio.run(Archiver(self.storage).cycle(drive))
        self.assertFalse(p.exists()); self.assertFalse(drive.deleted)
        raw=[r for r in drive.rows.values() if r['appProperties']['kind']=='raw'][0]
        self.assertEqual(raw['appProperties']['analysis_verified'],'false')
    def test_verified_analysis_allows_expiry_after_seven_days(self):
        p=self.raw(10); end=hour_end(p)
        self.storage.checkpoint('analysis_coverage_v1',{'start':end-7200,'through':end+7200})
        drive=FakeDrive();asyncio.run(Archiver(self.storage).cycle(drive))
        self.assertEqual(len(drive.deleted),1)
    def test_research_retains_recent_and_archives_old_before_purge(self):
        old=int((time.time()-31*86400)*1000); recent=int(time.time()*1000)
        self.storage.append('test',{'event_ts_ms':old});self.storage.append('test',{'event_ts_ms':recent});self.storage.flush()
        drive=FakeDrive();asyncio.run(Archiver(self.storage).cycle(drive))
        self.assertEqual(len(self.storage.query('test',0,recent+1)),1)
        self.assertTrue(any(r['appProperties']['kind']=='research' for r in drive.rows.values()))
    def test_checksum_mismatch_retains_local(self):
        p=self.raw();drive=FakeDrive()
        original=drive.request
        async def corrupt(method,url,**kw):
            row, headers=await original(method,url,**kw)
            return {**row,'md5Checksum':'bad'},headers
        drive.request=corrupt
        asyncio.run(Archiver(self.storage).cycle(drive))
        self.assertTrue(p.exists())
