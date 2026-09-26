import asyncio
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import threading
import unittest
from magi1.fast_observe import Observer,DAY,FIVE
from magi1.fast_bootstrap import initialize
from magi1.fast_repair import Repair


def payload(t,value=5):
    return {'candle_date_time_utc':datetime.fromtimestamp(t/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
            'opening_price':100,'high_price':100,'low_price':100,'trade_price':100,
            'candle_acc_trade_volume':value/100,'candle_acc_trade_price':value}


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.start=30*DAY+FIVE
        self.path=str(Path(self.tmp.name)/'obs.db')
        self.o=Observer(self.path,self.start);initialize(self.o.db)
        self.o.connected('healthy',['KRW-OK'],self.start-120000)
        self.o.connected('broken',['KRW-BAD'],self.start-120000)
        self.clock=self.start
        # Just the exact signal references; older unrelated history remains absent.
        for k in range(11):
            self.o.db.execute('INSERT INTO baseline VALUES(?,?,?)',('KRW-OK',self.start-FIVE-k*DAY,100 if k else 300))
        self.o.db.commit()
    def tearDown(self):self.o.db.close();self.tmp.cleanup()

    async def test_slow_repair_does_not_pause_healthy_stream_or_tracking(self):
        entered=threading.Event();release=threading.Event()
        class Slow:
            def candles(_,symbol,cursor):
                entered.set();release.wait(2)
                return [payload(cursor-FIVE)]
        r=Repair(self.o,['KRW-OK','KRW-BAD'],self.path,Slow(),clock=lambda:self.clock,emit=lambda x:None)
        ident=self.o.db.execute('INSERT INTO captures(ts,symbol,day,price,payload) VALUES(?,?,?,?,?)',(self.start-60000,'KRW-OK',30,100,'{}')).lastrowid
        self.o.active[ident]=(self.start-60000,'KRW-OK',100)
        task=asyncio.create_task(r.repair_symbol('KRW-BAD',self.start,max_pages=1))
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.001)
            self.assertTrue(entered.is_set());self.assertFalse(task.done())
            self.assertIn('KRW-BAD',self.o.repair_blocked)
            for i in range(10):
                t=self.start+i+1
                self.o.ingest(dict(type='trade',code='KRW-OK',trade_timestamp=t,sequential_id=i,
                                   trade_price=101,trade_volume=1,ask_bid='BID'),t)
                await asyncio.sleep(0)
            self.assertEqual(self.o.bar('KRW-OK')['count'],10)
            self.assertNotIn('KRW-OK',self.o.repair_blocked)
            self.assertAlmostEqual(self.o.db.execute('SELECT return_pct FROM outcomes').fetchone()[0],1)
            # No candle page may create/replace outcomes.
            before=self.o.db.execute('SELECT * FROM outcomes').fetchall()
        finally:release.set();await task
        self.assertEqual(before,self.o.db.execute('SELECT * FROM outcomes').fetchall())

    async def test_failed_market_does_not_stop_next_market(self):
        class Client:
            def candles(_,symbol,cursor):
                if symbol=='KRW-BAD':raise TimeoutError('test')
                return [payload(cursor-FIVE)]
        r=Repair(self.o,['KRW-BAD','KRW-OK'],self.path,Client(),clock=lambda:self.clock,emit=lambda x:None)
        await r.repair_symbol('KRW-BAD',self.start,max_pages=1)
        await r.repair_symbol('KRW-OK',self.start,max_pages=1)
        self.assertEqual(r.states['KRW-BAD'],'RETRY_WAIT')
        self.assertGreater(r.pages,0)
        self.assertNotIn('KRW-OK',self.o.repair_blocked)

    async def test_unblock_requires_fresh_confirmation(self):
        r=Repair(self.o,['KRW-BAD'],self.path,clock=lambda:self.clock,emit=lambda x:None)
        r.gate('KRW-BAD')
        self.o.previous['KRW-BAD']=(self.start-10000,True)
        for k in range(11):self.o.db.execute('INSERT INTO baseline VALUES(?,?,?)',('KRW-BAD',self.start-FIVE-k*DAY,100))
        self.assertTrue(r.gate('KRW-BAD'))
        self.assertNotIn('KRW-BAD',self.o.previous)

    async def test_symbol_corruption_does_not_clear_other_market(self):
        self.o.previous['KRW-OK']=(self.start,True)
        self.o.invalid_symbol('KRW-BAD',self.start)
        self.assertEqual(self.o.previous['KRW-OK'],(self.start,True))
        self.assertIn('healthy',self.o.groups)

if __name__=='__main__':unittest.main()
