import tempfile
import unittest
from pathlib import Path
from magi1.fast_observe import Observer, DAY, FIVE


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.start = 30 * DAY
        self.o = Observer(str(Path(self.tmp.name)/'obs.db'), self.start)
        self.o.connected('a', ['KRW-X'], self.start)

    def tearDown(self):
        self.o.db.close(); self.tmp.cleanup()

    def trade(self, t, ident=1, price=100):
        self.o.ingest(dict(type='trade', code='KRW-X', trade_timestamp=t, sequential_id=ident,
                           trade_price=price, trade_volume=1, ask_bid='BID', stream_type='REALTIME'), t)

    def test_duplicate_and_stale(self):
        self.trade(self.start+1); self.trade(self.start+2)
        self.assertEqual(self.o.bar('KRW-X')['count'], 1)
        self.o.ingest(dict(type='trade', code='KRW-X', trade_timestamp=self.start), self.start+3000)
        self.assertEqual(self.o.bar('KRW-X')['count'], 1)

    def test_no_partial_or_gap_baseline(self):
        for t in range(self.start+10000, self.start+FIVE+1, 10000):
            self.o.advance(t)
        self.assertEqual(self.o.db.execute('SELECT count(*) FROM baseline').fetchone()[0], 1)
        self.o.gap('a', self.start+FIVE+1, 'test')
        self.o.connected('a', ['KRW-X'], self.start+FIVE+20000)
        for t in range(self.start+FIVE+10000, self.start+2*FIVE+1, 10000):
            self.o.advance(t)
        self.assertEqual(self.o.db.execute('SELECT count(*) FROM baseline').fetchone()[0], 1)

    def test_outcome_is_first_after_horizon_and_missing(self):
        c = self.o.db.execute('INSERT INTO captures(ts,symbol,day,price,payload) VALUES(?,?,?,?,?)',
                             (self.start,'KRW-X',30,100,'{}')).lastrowid
        self.o.active[c] = (self.start,'KRW-X',100)
        self.o.mark_outcomes('KRW-X',self.start+59000,200)
        self.o.mark_outcomes('KRW-X',self.start+61000,101)
        self.o.mark_outcomes('KRW-X',self.start+62000,110)
        self.assertAlmostEqual(self.o.db.execute('SELECT return_pct FROM outcomes').fetchone()[0],1)
        self.o.advance(self.start+920000)
        self.assertEqual(dict(self.o.db.execute('SELECT status,count(*) FROM outcomes GROUP BY status')), {'OBSERVED':1,'MISSING':3})

    def test_capture_two_nonoverlap_windows_and_restart(self):
        # Deterministic causal fixture; not imported historical/live baseline.
        for k in range(1,11):
            self.o.db.execute('INSERT INTO baseline VALUES(?,?,?)', ('KRW-X',self.start-k*DAY,100))
        self.o.db.execute('INSERT INTO baseline VALUES(?,?,?)', ('KRW-X',self.start,300))
        self.o.current = self.start + FIVE + 60000
        q = self.o.bars['KRW-X']
        for i in range(6):
            q.append(dict(t=self.o.current-60000+i*10000,value=1,buy=1,count=1,ofi=1))
        def window(amount):
            b=self.o.bar('KRW-X'); b.update(value=amount,buy=amount,count=20,ofi=1)
            end=self.o.current+10000
            self.o.books['KRW-X']=dict(ts=end-1,bp=99,ap=101)
            self.o.last_price['KRW-X']=(end-1,100,end-1)
            self.o.advance(end)
        window(30)
        self.assertEqual(self.o.report()['captures'],0)
        window(90)
        self.assertEqual(self.o.report()['captures'],1)
        self.o.previous.clear()
        window(200);window(1000)
        self.assertEqual(self.o.report()['captures'],1)
        path=str(Path(self.tmp.name)/'obs.db')
        other=Observer(path,self.o.current)
        self.assertEqual(len(other.active),1)
        self.assertEqual(other.report()['captures'],1)
        other.db.close()

    def test_missing_history_never_captures(self):
        for t in range(self.start+10000,self.start+900000,10000):
            self.o.advance(t)
        self.assertEqual(self.o.report()['captures'],0)
        self.assertIn('BASELINE_WARMUP', self.o.db.execute("SELECT payload FROM events WHERE kind='ASSESSMENT' LIMIT 1").fetchone()[0])

if __name__=='__main__': unittest.main()
