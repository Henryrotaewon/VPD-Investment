import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from magi2.fast_monitor import FastMonitor,Watch
from magi3.fast_audit import FastAudit,order_result
from magi3.fast_execution import FastBudget,UpbitFastOrders

class FastAuditTests(unittest.TestCase):
    def test_signal_criteria_and_no_order_persist_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            monitor=FastMonitor(root,lambda _:None)
            row={'price':100,'rank':2,'previous_rank':9,'rank_jump':7,'returns_bps':{'5':150},'asset':'BTC','quote_currency':'KRW'}
            watch=Watch(row,0)
            for ts in range(0,30000,1000):watch.quote(ts,100,100.01)
            self.assertTrue(watch.quote(30000,100.1,100.11))
            monitor.record_signal('upbit','KRW-BTC',watch,30000,100)
            monitor.audit.db.close()
            audit=FastAudit(Path(root)/'fast_evidence.sqlite3')
            rows=list(audit.export(until_ms=30001));audit.db.close()
            self.assertEqual([r['event_type'] for r in rows],['SIGNAL_DETECTED','ORDER_SKIPPED'])
            self.assertEqual(rows[0]['signal_id'],rows[1]['signal_id'])
            self.assertEqual(rows[0]['observed']['rank_jump'],7)
            self.assertEqual(rows[0]['criteria']['max_spread_bps'],15)
            self.assertGreater(rows[0]['observed']['breakout_bps'],5)
            self.assertEqual(rows[0]['duration_ms'],100)
            self.assertIsNone(rows[0]['exchange_ts_ms'])
            self.assertFalse(rows[1]['result']['submitted'])
    def test_order_request_unknown_then_reconcile_same_id(self):
        with tempfile.TemporaryDirectory() as root:
            budget=FastBudget(Path(root)/'orders.db');adapter=Mock();adapter._auth.return_value={'Authorization':'never-log-me'}
            adapter.http.post.side_effect=TimeoutError
            client=UpbitFastOrders(adapter,budget,enabled=True)
            snap={'complete':True,'observed_ts_ms':1000,'total_equity_krw':1000000,
                  'available_cash_krw':100000,'fast_position_value_krw':0,'external_fast_pending_krw':0}
            with patch('magi3.fast_execution.time.time_ns',return_value=1000000000):
                result=client.buy('signal-one','KRW-BTC',100,10000,snap,.001,5000)
            response=adapter.http.get.return_value
            response.json.return_value={'state':'cancel','executed_volume':'20','remaining_volume':'80','paid_fee':'1',
                                        'Authorization':'never-log-me','trades':[{'price':'100','volume':'20','funds':'2000','secret':'never-log-me'}]}
            client.reconcile(result['identifier'])
            rows=list(budget.audit.export());budget.close()
            self.assertEqual([r['event_type'] for r in rows],['ORDER_REQUEST','ORDER_OUTCOME_UNKNOWN','ORDER_RECONCILED'])
            self.assertEqual({r['order_id'] for r in rows},{result['identifier']})
            self.assertEqual(rows[-1]['result']['executed_volume'],'20')
            self.assertNotIn('never-log-me',json.dumps(rows))
    def test_nonfinite_rejected_and_export_time_filter(self):
        with tempfile.TemporaryDirectory() as root:
            a=FastAudit(Path(root)/'a.db')
            with self.assertRaises(ValueError):a.record('X','s','upbit','KRW-BTC','ALERT_ONLY',observed={'price':float('nan')})
            a.record('X','s','upbit','KRW-BTC','ALERT_ONLY',ts_ms=1000)
            self.assertEqual(list(a.export(1001,2000)),[])
            a.db.close()
