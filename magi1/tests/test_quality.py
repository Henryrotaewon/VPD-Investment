import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from magi1.quality import QuoteCoverage,outcome,cleanup_invalid
from magi1.storage import Storage
from magi1.research import ResearchEngine
from magi1.normalizer import book,trade
from magi1.compact import meaningful_rows

class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Storage(self.tmp.name)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def event(self,ts,bid=100,ask=101):return book('upbit','BTC-KRW',[[bid,1]],[[ask,1]],ts,received_ts_ms=ts)
    def test_quotes_without_trades_are_evaluable_and_spread_is_cost(self):
        e=ResearchEngine(self.s,['BTC'],0);e.ingest(self.event(0))
        e.entry('x','BTC','BUY',0,100,['FLOW_ONLY'],{},'MICRO')
        for t in range(1000,11000,1000):e.ingest(self.event(t));e.tick(t)
        rows=self.s.query('evaluation',0,11000)
        self.assertEqual(len(rows),1);self.assertAlmostEqual(rows[0]['forward_return'],100/101-1)
        self.assertEqual(rows[0]['evaluation_version'],'quote-v2')
    def test_stale_quote_blocks_entry(self):
        e=ResearchEngine(self.s,['BTC'],0);e.ingest(self.event(0))
        e.entry('x','BTC','BUY',5000,100,['FLOW_ONLY'],{},'MICRO')
        self.assertFalse(e.pending)
    def test_gap_not_turned_into_good_return_and_denominator_retained(self):
        e=ResearchEngine(self.s,['BTC'],0);e.ingest(self.event(0));e.entry('x','BTC','BUY',0,100,['FLOW_ONLY'],{},'MICRO')
        e.tick(1000);e.ingest(self.event(9000));e.tick(10000)
        self.assertFalse(self.s.query('evaluation',0,11000));self.s.flush()
        self.assertEqual(sum(v for k,v in self.s.restore('quality_counts_v2').items() if 'EVALUATION_MISSING_DATA' in k),1)
    def test_duplicate_trade_not_written_twice(self):
        from dataclasses import replace
        e=ResearchEngine(self.s,['BTC'],0)
        x=replace(trade('upbit','BTC-KRW',100,1,0,'BUY',received_ts_ms=0),trade_id='a')
        y=replace(x,trade_id='b',received_ts_ms=1)
        e.ingest(x);e.ingest(y);e.ingest(replace(x,received_ts_ms=2))
        self.s.flush();self.assertEqual(self.s.raw_stats['kinds']['trade']['count'],2)
    def test_unchanged_book_heartbeat_changed_books_preserved(self):
        e=ResearchEngine(self.s,['BTC'],0)
        for t in [0,100,200,1000]:e.ingest(self.event(t))
        e.ingest(self.event(1001,99,102));self.s.flush()
        self.assertEqual(self.s.raw_stats['kinds']['book']['count'],3)
    def test_cleanup_preserves_valid_and_counts_missing_idempotently(self):
        self.s.append('evaluation',{'event_ts_ms':1000,'status':'COMPLETE','asset':'BTC'})
        self.s.append('evaluation',{'event_ts_ms':1000,'status':'MISSING_DATA','asset':'BTC'})
        self.s.append('propagation_missing',{'event_ts_ms':1000,'asset':'BTC'})
        self.s.flush();first=cleanup_invalid(self.s,2000);second=cleanup_invalid(self.s,3000)
        self.assertEqual(first,second);self.assertEqual(first['deleted'],{'evaluation':1,'propagation_missing':1})
        self.assertEqual(len(self.s.query('evaluation',0,3000)),1)
        self.assertEqual(sum(self.s.restore('quality_counts_v2').values()),2)
    def test_restart_does_not_join_discontinuous_quotes(self):
        e=ResearchEngine(self.s,['BTC'],0);e.ingest(self.event(0));e.entry('x','BTC','BUY',0,100,['FLOW_ONLY'],{},'MICRO');e.checkpoint()
        restarted=ResearchEngine(self.s,['BTC'],5000)
        self.assertFalse(restarted.pending)
    def test_snapshot_multiset_does_not_discard_unique_valid_rows(self):
        self.s.append('evaluation',{'status':'COMPLETE','event_ts_ms':1});self.s.flush()
        self.s.db.execute('PRAGMA wal_checkpoint(FULL)')
        path=Path(self.tmp.name)/'copy.gz'
        with gzip.open(path,'wb') as out:out.write((Path(self.tmp.name)/'research.db').read_bytes())
        first=meaningful_rows(path,Path(self.tmp.name)/'check.db')
        self.s.append('evaluation',{'status':'COMPLETE','event_ts_ms':2});self.s.flush();self.s.db.execute('PRAGMA wal_checkpoint(FULL)')
        with gzip.open(path,'wb') as out:out.write((Path(self.tmp.name)/'research.db').read_bytes())
        second=meaningful_rows(path,Path(self.tmp.name)/'check.db')
        self.assertTrue(first<=second);self.assertFalse(second<=first)

class CompactSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_relink_failure_prevents_snapshot_delete(self):
        from unittest.mock import patch
        from collections import Counter
        from magi1.compact import compact_backups
        import hashlib
        data=b'compressed-test'; md5=hashlib.md5(data).hexdigest()
        class Content:
            async def iter_chunked(self,n):yield data
        class Response:
            status=200;content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        class Session:
            def get(self,*args,**kwargs):return Response()
        class Drive:
            session=Session();token='test';deleted=[]
            async def list(self,q):
                if "value='research'" in q:return [{'id':'old','name':'research-old.db.gz','size':len(data),'md5Checksum':md5}]
                return [{'id':'raw','appProperties':{'research_id':'old'}}]
            async def request(self,method,url,**kwargs):
                if method=='DELETE':self.deleted.append(url)
                if url.endswith('/new'):return {'id':'new','md5Checksum':md5},{}
                if method=='PATCH':raise RuntimeError('Drive relink failed')
                return {},{}
        drive=Drive()
        with tempfile.TemporaryDirectory() as d, patch('magi1.compact.meaningful_rows',return_value=Counter({b'same':1})):
            with self.assertRaises(RuntimeError):
                await compact_backups(drive,'folder',{'id':'new','md5Checksum':md5},Path(d)/'current',Path(d))
        self.assertFalse(drive.deleted)
