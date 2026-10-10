import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from magi2 import capture_audit as audit
from magi2 import server_runner as server


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.start=audit.stamp('2026-10-10T09:00:00+09:00')
        path=audit.ledger_path(self.root,'fast');path.parent.mkdir(parents=True)
        self.path=path
        self.db=sqlite3.connect(path)
        self.db.executescript('''
          CREATE TABLE fills(id INTEGER PRIMARY KEY,ts INTEGER,symbol TEXT,side TEXT,quantity REAL,
            price REAL,fee REAL,cash REAL,pnl REAL,reason TEXT,decision_ms INTEGER);
          CREATE TABLE events(id INTEGER PRIMARY KEY,ts INTEGER,kind TEXT,symbol TEXT,payload TEXT);
          CREATE TABLE signals(symbol TEXT,day INTEGER,ts INTEGER,status TEXT,reason TEXT,payload TEXT);
        ''')

    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def fill(self,ident,t,side,qty,price,cash,pnl):
        self.db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (ident,self.start+t,'KRW-BFC',side,qty,price,.1,cash,pnl,'PROTECTION',self.start+t-200))
        self.db.commit()

    def event(self,t,kind,p):
        self.db.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                        (self.start+t,kind,'KRW-BFC',json.dumps(p)));self.db.commit()

    def test_partial_sells_and_reentry_diagnostics_never_merge(self):
        self.fill(1,1000,'BUY',10,40,-401,0)
        self.fill(2,2000,'SELL',4,39,155.8,-4.6)
        self.fill(3,3000,'SELL',6,39,233.7,-6.9)
        self.fill(4,4000,'BUY',10,41,-411,0)
        self.fill(5,5000,'SELL',10,42,419,8)
        self.event(1000,'ENTRY_DIAGNOSTIC',dict(entry_ms=self.start+1000,stop_distance_bps=200))
        self.event(3000,'EXIT_DIAGNOSTIC',dict(entry_ms=self.start+1000,continuous=True,best_exit_net_pct=.2))
        self.event(4000,'ENTRY_DIAGNOSTIC',dict(entry_ms=self.start+4000,stop_distance_bps=100))
        self.event(6000,'POST_EXIT_DIAGNOSTIC',dict(entry_ms=self.start+1000,horizon_ms=60000,status='MISSING'))
        before=self.path.read_bytes()
        rows=audit.fast_trades(self.root,self.start,self.start+10000)
        self.assertEqual([r['net_pnl'] for r in rows],[8,-11.5])
        self.assertAlmostEqual(rows[1]['initial_stop'],39.2)
        self.assertEqual(rows[0]['post_exit'],[])
        self.assertEqual(len(rows[1]['post_exit']),1)
        self.assertEqual(rows[1]['holding_ms'],2000)
        self.assertEqual(self.path.read_bytes(),before)

    def test_partial_exit_is_not_a_completed_trade(self):
        self.fill(1,1000,'BUY',10,40,-401,0)
        self.fill(2,2000,'SELL',4,39,155.8,-4.6)
        r=audit.fast_trades(self.root,self.start,self.start+3000)[0]
        self.assertFalse(r['complete']);self.assertIsNone(r['net_return_pct'])
        self.assertIsNone(r['exit_ms'])

    def test_trading_day_and_schedule_boundaries(self):
        end=self.start
        self.assertEqual(audit.closed_session(end+9*audit.MINUTE),(end-2*audit.DAY,end-audit.DAY))
        self.assertEqual(audit.closed_session(end+10*audit.MINUTE),(end-audit.DAY,end))
        self.assertEqual(audit.closed_session(end+23*3600000),(end-audit.DAY,end))

    def test_missing_source_is_unknown_not_no_capture_or_zero(self):
        archive=audit.Evidence(self.root)
        r=audit.strategy_evidence(self.root,archive,'indicator','KRW-BFC',self.start,self.start+audit.DAY,None,None)
        self.assertEqual(r['status'],'확인 대기')
        r=audit.strategy_evidence(self.root,archive,'fast','KRW-BFC',self.start,self.start+audit.DAY,None,29)
        self.assertEqual(r['status'],'미매수·사유 미확인')
        self.assertFalse(audit.ledger_path(self.root,'indicator').exists())
        archive.db.close()

    def test_recorded_pause_at_surge_precedes_later_resume(self):
        self.event(-1000,'USER_PAUSED',{})
        self.event(300000,'USER_RESUMED',{})
        archive=audit.Evidence(self.root)
        r=audit.strategy_evidence(self.root,archive,'fast','KRW-BFC',self.start,self.start+audit.DAY,self.start,29)
        self.assertEqual(r['basis'],'RECORDED_PAUSE')
        archive.db.close()

    def test_archive_dedup_and_retention_leave_source_unchanged(self):
        self.event(1000,'ENTRY_CANCELED',dict(reason='ENTRY_PRICE_CAP'))
        archive=audit.Evidence(self.root);before=self.path.read_bytes()
        archive.sync(self.start+2000);archive.sync(self.start+2000)
        rows=archive.rows('fast','KRW-BFC',self.start,self.start+3000)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['payload']['reason'],'ENTRY_PRICE_CAP')
        archive.sync(self.start+4*audit.DAY)
        self.assertEqual(archive.rows('fast','KRW-BFC',self.start,self.start+3000),[])
        self.assertEqual(self.path.read_bytes(),before)
        archive.db.close()

    def test_ranking_excludes_forming_and_reports_missing_market(self):
        public=audit.Public();end=self.start
        def get(path,params=None):
            if path=='market/all':return [{'market':'KRW-BFC'},{'market':'KRW-NEW'},{'market':'BTC-X'}]
            self.assertEqual(params['to'],audit.iso(end))
            if params['market']=='KRW-NEW':return []
            return [{'candle_date_time_utc':audit.iso(end-audit.DAY).removesuffix('+00:00'),
                     'prev_closing_price':10,'high_price':12,'trade_price':11},
                    {'candle_date_time_utc':audit.iso(end-2*audit.DAY).removesuffix('+00:00'),
                     'trade_price':10},
                    {'candle_date_time_utc':audit.iso(end).removesuffix('+00:00'),
                     'prev_closing_price':11,'high_price':100,'trade_price':100}]
        public.get=get
        rows,coverage=public.ranking(end-audit.DAY,end)
        self.assertEqual(len(rows),1);self.assertAlmostEqual(rows[0]['peak_pct'],20)
        self.assertEqual(coverage['missing'],['KRW-NEW'])

    def test_listing_reference_is_not_a_verified_previous_close(self):
        public=audit.Public();end=self.start
        public.get=lambda path,params=None: ([{'market':'KRW-NEW'}] if path=='market/all' else
            [{'candle_date_time_utc':audit.iso(end-audit.DAY).removesuffix('+00:00'),
              'prev_closing_price':67,'high_price':92.3,'trade_price':82.9}])
        rows,coverage=public.ranking(end-audit.DAY,end)
        self.assertEqual(rows,[])
        self.assertEqual(coverage['ready'],0)
        self.assertEqual(coverage['missing'],['KRW-NEW'])

    def test_daily_delivery_claim_survives_restart_and_uncertain_send(self):
        archive=audit.Evidence(self.root);send=Mock(side_effect=TimeoutError())
        service=audit.Service(self.root,Mock(),send)
        report=dict(start_ms=self.start-audit.DAY,end_ms=self.start,rows=[],coverage=dict(ready=1,universe=1,missing=[]))
        service.deliver(archive,report)
        archive.db.close();archive=audit.Evidence(self.root)
        audit.Service(self.root,Mock(),send).deliver(archive,report)
        send.assert_called_once()
        self.assertEqual(archive.db.execute('SELECT status FROM deliveries').fetchone()[0],'SEND_UNCERTAIN')
        archive.db.close()

    def test_read_only_menu_and_authorization(self):
        with patch.object(server,'STATE_DIR',self.root),patch.object(server,'telegram') as send, \
             patch.object(server,'start_engine') as trade,patch.object(server,'telegram_api'), \
             patch.object(server,'ALLOWED_CHAT_ID','7'):
            server.handle_command('급등 포착 점검','7','7')
            self.assertIn('09:10',send.call_args.args[0])
            server.handle_callback(dict(id='x',data='nav:fast_trade_audit',message=dict(chat=dict(id='7'))))
            self.assertIn('inline_keyboard',send.call_args.args[1])
            send.reset_mock()
            server.handle_callback(dict(id='x',data='audit_trade:1',message=dict(chat=dict(id='8'))))
            send.assert_not_called();trade.assert_not_called()


if __name__=='__main__':unittest.main()
