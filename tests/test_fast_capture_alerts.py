import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
from magi1.fast_observe import Observer
from magi1.fast_dayhold import DayHoldPaper
from magi2.fast_capture_alerts import Service,capture_text

T=20000*86400000


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.directory=self.root/'fast-observe'
        self.o=Observer(self.directory/'observe.sqlite3',T)
        self.send=Mock();self.now=T+100
        self.service=Service(self.root,lambda s:None,self.send,lambda:self.now)
    def tearDown(self):self.o.db.close();self.tmp.cleanup()
    def capture(self,ts,symbol='KRW-X'):
        cur=self.o.db.execute('INSERT INTO captures(ts,symbol,day,price,payload) VALUES(?,?,?,?,?)',
                             (ts,symbol,ts//86400000,100,json.dumps(dict(relative_value=3,burst=4,buy_share=.7))))
        self.o.db.commit();return cur.lastrowid
    def test_new_capture_only_once_across_restart(self):
        self.capture(T+1,'KRW-OLD');self.capture(T+101)
        self.now=T+200;self.service.poll();self.service.poll()
        self.assertEqual(self.send.call_count,1)
        text,markup=self.send.call_args.args
        self.assertIn('KRW-X',text);self.assertNotIn('KRW-OLD',text)
        self.assertIn('포착 알림',text);self.assertIn('여부 확인 대기',text)
        Service(self.root,lambda s:None,self.send,lambda:self.now).poll()
        self.assertEqual(self.send.call_count,1)
    def test_stale_future_and_missing_venue_do_not_send(self):
        self.capture(T+101);self.capture(T+1000000,'KRW-FUTURE')
        self.now=T+200000;self.service.poll()
        self.send.assert_not_called()
    def test_failed_delivery_is_recorded_without_duplicate_retry(self):
        self.capture(T+101);self.now=T+200
        self.send.side_effect=TimeoutError('unknown delivery')
        self.service.poll();self.service.poll()
        self.assertEqual(self.send.call_count,1)
        with sqlite3.connect(self.service.path) as db:
            self.assertEqual(db.execute('SELECT status FROM delivery').fetchone()[0],'DELIVERY_UNCONFIRMED')
    def test_same_symbol_different_venue_independent_alert(self):
        self.capture(T+101)
        d=self.directory/'venues/bithumb';o=Observer(d/'observe.sqlite3',T)
        o.db.execute('INSERT INTO captures(ts,symbol,day,price,payload) VALUES(?,?,?,?,?)',(T+101,'KRW-X',T//86400000,101,'{}'))
        o.db.commit();o.db.close();self.now=T+200;self.service.poll()
        self.assertEqual(self.send.call_count,2)
        self.assertIn('빗썸',self.send.call_args.args[0])
    def test_pending_alert_not_misrepresented_as_filled(self):
        ident=self.capture(T+101)
        p=DayHoldPaper(self.directory/'paper-v1.sqlite3',T)
        p.set_regime(dict(state='NEUTRAL',ts=T))
        p.signal('KRW-X',T+102,100,98,{})
        row=self.o.db.execute('SELECT id,ts,symbol,price,payload FROM captures WHERE id=?',(ident,)).fetchone()
        text=capture_text(row,'upbit',self.directory/'paper-v1.sqlite3')
        self.assertIn('모의매수 대기',text);self.assertIn('TP +12% / SL −4%',text)
        self.assertNotIn('모의매수 체결 ·',text);p.db.close()


if __name__=='__main__':unittest.main()
