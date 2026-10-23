import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zlib

from magi1.fast_venue import Decoder, VenueObserver, account, CandleClient
from magi1.fast_bootstrap import put, missing_plan, apply_page
from magi1.fast_candle_store import enable, compact, historical, known
from magi1.fast_reference import reference_state
from magi1.fast_observe import DAY, FIVE
from magi2.fast_observe_service import Service
from magi2.fast_venue_report import result_lines

T=20000*DAY+120*FIVE


class CompactHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.o=VenueObserver(Path(self.tmp.name)/'observe.db',T,'bithumb')
        self.db=self.o.db
    def tearDown(self):self.db.close();self.tmp.cleanup()

    def test_exact_ten_day_ratio_unknown_and_zero_survive_compaction(self):
        full=T-FIVE
        self.db.execute('INSERT INTO recent5m VALUES(?,?,?,?,?,?,?,?,?)',('KRW-Q',full,1,1,1,1,1,200,'API'))
        for k in range(1,11):
            self.db.execute('INSERT INTO baseline VALUES(?,?,?)',('KRW-Q',full-k*DAY,100))
        self.db.execute('INSERT INTO baseline VALUES(?,?,?)',('KRW-ZERO',full-DAY,0))
        compact(self.db,T)
        self.assertEqual(self.db.execute('SELECT count(*) FROM baseline').fetchone()[0],0)
        self.assertEqual(reference_state(self.db,'KRW-Q',T)['relative_value'],2.)
        self.assertEqual(historical(self.db,'KRW-ZERO',[full-DAY]),{full-DAY:0.})
        self.assertEqual(historical(self.db,'KRW-ZERO',[full-2*DAY]),{})
        self.assertIn('HISTORY_INCOMPLETE',reference_state(self.db,'KRW-ZERO',T)['reasons'])

    def test_validated_page_survives_repair_and_unknown_prelisting_is_not_zero(self):
        rows=[[T-2*FIVE,1,1,1,1,1,5]]
        covered=apply_page(self.db,'KRW-Q',T,rows,T,{T-3*FIVE,T-2*FIVE,T-FIVE})
        self.assertEqual(covered,{T-2*FIVE,T-FIVE})
        self.assertEqual(historical(self.db,'KRW-Q',[T-2*FIVE,T-FIVE]),{T-2*FIVE:5.,T-FIVE:0.})
        self.assertNotIn(T-2*FIVE,missing_plan(self.db,'KRW-Q',T))
        self.assertIn(T-3*FIVE,missing_plan(self.db,'KRW-Q',T))

    def test_separate_databases_do_not_mix_same_symbol(self):
        other=sqlite3.connect(':memory:')
        enable(other)
        self.db.execute('INSERT INTO baseline VALUES(?,?,?)',('KRW-Q',T-DAY,100))
        compact(self.db,T)
        self.assertEqual(historical(other,'KRW-Q',[T-DAY]),{})
        other.close()


class DecodeTests(unittest.TestCase):
    def test_bithumb_candle_boundary_is_exclusive_kst_without_utc_suffix(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        client=CandleClient('bithumb');calls=[]
        client.get=lambda url,params:calls.append(params) or []
        client.candles('KRW-Q',T)
        self.assertEqual(calls[0]['to'],datetime.fromtimestamp(T/1000,ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertFalse(calls[0]['to'].endswith('Z'))

    def test_bithumb_native_trade_side_and_microseconds(self):
        d=Decoder('bithumb')
        row=d.decode(dict(type='trade',code='KRW-Q',trade_timestamp=T*1000,
            sequential_id=12,trade_price=100,trade_volume=2,ask_bid='BID'),T)[0]
        self.assertEqual(row['trade_timestamp'],T)
        self.assertEqual(row['ask_bid'],'BID')

    def test_binance_sell_aggressor_and_depth_sequence(self):
        d=Decoder('binance')
        trade=d.decode({'data':dict(e='trade',s='BTCUSDT',T=T,t=7,p='100',q='2',m=True)},T)[0]
        self.assertEqual(trade['ask_bid'],'ASK')
        message=dict(stream='btcusdt@depth5',data=dict(lastUpdateId=2,bids=[['99','2']],asks=[['100','3']]))
        book=d.decode(message,T)[0]
        self.assertEqual(book['timestamp_basis'],'RECEIVE_TIME')
        self.assertEqual(book['code'],'BTCUSDT')
        self.assertEqual(d.decode(message,T+1),[])

    def test_kraken_book_checksum_deletions_and_trade_snapshot(self):
        d=Decoder('kraken')
        def checksum(asks,bids):
            def digits(x):return format(Decimal(x),'f').replace('.','').lstrip('0')
            return zlib.crc32(''.join(digits(p)+digits(q) for p,q in asks+bids).encode())
        row=dict(symbol='BTC/USD',timestamp='2026-10-18T06:00:00Z',
                 asks=[dict(price=Decimal('100.0'),qty=Decimal('3.0'))],
                 bids=[dict(price=Decimal('99.0'),qty=Decimal('2.0'))],
                 checksum=checksum([('100.0','3.0')],[('99.0','2.0')]))
        self.assertEqual(len(d.decode(dict(channel='book',type='snapshot',data=[row]),T)),1)
        row.update(asks=[dict(price=Decimal('100.0'),qty=Decimal('0.0')),
                         dict(price=Decimal('101.0'),qty=Decimal('1.0'))],bids=[],
                   checksum=checksum([('101.0','1.0')],[('99.0','2.0')]))
        self.assertEqual(d.decode(dict(channel='book',type='update',data=[row]),T)[0]['orderbook_units'][0]['ask_price'],101.)
        row['checksum']=0
        with self.assertRaisesRegex(ValueError,'CHECKSUM'):
            d.decode(dict(channel='book',type='update',data=[row]),T)
        self.assertEqual(d.decode(dict(channel='trade',type='snapshot',data=[]),T),[])

    def test_native_kraken_720_limit_does_not_generate_old_candles(self):
        client=CandleClient('kraken')
        client.kraken_pairs={'BTC/USD':'XBTUSD'}
        client.get=lambda *args:dict(result={'BTCUSD':[[T//1000,'100','101','99','100','100','1',2]],'last':T//1000})
        self.assertEqual(client.candles('BTC/USD',T-DAY),[])


class AccountTests(unittest.TestCase):
    def test_foreign_budget_and_restart_preserve_native_currency_account(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'paper.db'
            paper=account(path,T,'binance',dict(bid=1300,ask=1300,ts=T))
            self.assertAlmostEqual(paper.s['cash'],3000000/1300)
            paper.set_regime(dict(state='NEUTRAL',ts=T))
            paper.signal('BTCUSDT',T+1,100,99,{})
            self.assertIn('BTCUSDT',paper.s['pending'])
            original=paper.policy
            paper.db.close()
            paper=account(path,T+2,'binance',dict(bid=1500,ask=1500,ts=T+2))
            self.assertEqual(paper.policy,original)
            self.assertAlmostEqual(paper.s['cash'],3000000/1300)
            paper.db.close()

    def test_all_four_workers_started_and_stop_is_propagated(self):
        from magi2.telegram_ui import parse_command
        self.assertEqual(parse_command('fast_venues'),'fast_venues')
        with tempfile.TemporaryDirectory() as root,patch('magi2.fast_observe_service.threading.Thread') as thread:
            service=Service(root,lambda x:None)
            service.start()
            self.assertEqual([s.venue for s in service.children],['bithumb','binance','kraken'])
            self.assertEqual(thread.call_count,4)
            service.stop()
            self.assertTrue(all(s.stopped.is_set() for s in service.children))

    def test_stale_foreign_fx_not_reported_as_current_krw_equity(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'fast-observe/venues/binance'
            p.mkdir(parents=True)
            data=dict(observation_ms=T,observation=dict(paper=dict(equity=2000,closed=0,winning=0),
                    paper_policy=dict(initial_krw=3000000),fx=dict(bid=1500,ts=T)))
            (p/'status.json').write_text(json.dumps(data))
            self.assertIn('3,000,000원',' '.join(result_lines(root,T)))
            self.assertIn('환율 갱신 대기',' '.join(result_lines(root,T+180001)))


if __name__=='__main__':unittest.main()
