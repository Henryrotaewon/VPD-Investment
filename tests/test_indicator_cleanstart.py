import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from magi2.hourly_indicator import (HourlyLedger,HourlyMonitor,PublicCandles,
    DAY,HOUR,STEP,iso)
from magi2.indicator_reset import RESET_ID,reset_once
from magi2.paper_performance import indicator_results

T=200*DAY+HOUR


def candle(t,price=100):
    return dict(candle_date_time_utc=iso(t).removesuffix('Z'),opening_price=price,
                high_price=price,low_price=price,trade_price=price,candle_acc_trade_volume=1)


class FillTests(unittest.TestCase):
    def market(self,rows):
        market=PublicCandles();calls=[]
        def get(path,params):
            calls.append((path,params))
            return list(reversed([r for r in rows if r['candle_date_time_utc']<params['to'].removesuffix('Z')]))[:params['count']]
        market.get=get
        return market,calls

    def test_old_exit_uses_first_actual_candle_not_latest_200(self):
        rows=[candle(T+i*STEP,100+i) for i in range(1200)]
        market,calls=self.market(rows)
        bar=market.fills('KRW-A',T,T+1200*STEP)
        self.assertEqual(bar[:2],[T,100])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1]['to'],iso(T+200*STEP))

    def test_no_trade_gaps_cross_windows_without_fake_or_skipped_fill(self):
        rows=[candle(T-STEP),candle(T+400*STEP,123),candle(T+405*STEP,456)]
        market,calls=self.market(rows)
        self.assertEqual(market.fills('KRW-A',T,T+500*STEP)[:2],[T+400*STEP,123])
        self.assertEqual(len(calls),3)

    def test_short_window_small_payload_future_and_invalid_data_guards(self):
        market,calls=self.market([candle(T),candle(T+STEP,999)])
        self.assertEqual(market.fills('A',T,T+1000)[:2],[T,100])
        self.assertEqual(calls[0][1]['count'],1)
        self.assertIsNone(market.fills('A',T,T))
        with self.assertRaisesRegex(ValueError,'BOUNDARY'):market.fills('A',T+1,T+STEP)
        market.get=lambda *a:[candle(T+STEP)]
        with self.assertRaisesRegex(ValueError,'WINDOW'):market.fills('A',T,T+1000)
        bad=candle(T);bad['low_price']=101
        market.get=lambda *a:[bad]
        with self.assertRaisesRegex(ValueError,'OHLCV'):market.fills('A',T,T+1000)


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.l=HourlyLedger(self.root,T)
        self.l.s.update(cash=2700000,realized=-25,scan={'errors':{'old':6}})
        self.l.s['positions']['KRW-A']=dict(entry_ms=T,cost=300000,qty=3,reference=100000,initial_stop=90000)
        self.l.schedule_exit('KRW-A',T+1000,'TEST',T)
        self.l.event(T,'SELL','OLD',{'pnl':-25,'fill_ms':T})
        self.l.mark({},T)
        self.market=Mock();self.market.get.return_value=[candle(T+(i+500)*STEP) for i in range(200)]
        self.logs=[]
    def tearDown(self):self.l.db.close();self.tmp.cleanup()

    def test_reset_removes_only_indicator_data_and_is_durable_once(self):
        others=['paper_state.json','paper_events.jsonl','paper_performance.sqlite3','fast_paper.sqlite3']
        for name in others:(self.root/name).write_bytes(b'untouched')
        oldorder=copy.deepcopy(self.l.pending()['KRW-A'])
        self.assertTrue(reset_once(self.l,RESET_ID,T+1000*STEP,self.logs.append,self.market))
        self.assertTrue(any('FILL_WINDOW_MISSING' in line for line in self.logs))
        self.assertEqual(self.l.s['cash'],3000000)
        self.assertEqual(self.l.s['realized'],0)
        for key in ('positions','pending','scan','previous','confirmed','prices','used_day','last_exit'):
            self.assertFalse(self.l.s[key])
        for table in ('events','marks'):
            self.assertEqual(self.l.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assertFalse(self.l.execute('KRW-A',oldorder,[T+STEP,100],T+2000*STEP))
        for name in others:self.assertEqual((self.root/name).read_bytes(),b'untouched')
        results=indicator_results(self.root,T+1000*STEP)
        self.assertFalse(results.trades);self.assertFalse(results.marks)
        # Reopening with the same still-set environment request must not reset
        # any new observations/marks or move the experiment start time.
        self.l.mark({},T+1000*STEP)
        started=self.l.s['started_ms'];self.l.db.close()
        with patch.dict('os.environ',{'MAGI2_INDICATOR_RESET_ONCE':RESET_ID}):
            self.l=HourlyLedger(self.root,T+1001*STEP)
        self.assertEqual(self.l.s['started_ms'],started)
        self.assertEqual(self.l.db.execute('SELECT count(*) FROM marks').fetchone()[0],1)

    def test_transaction_failure_preserves_old_state_events_marks_and_no_receipt(self):
        before=copy.deepcopy(self.l.s)
        counts=[self.l.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('events','marks')]
        self.l.db.execute("CREATE TRIGGER fail_reset BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaisesRegex(Exception,'test failure'):
            reset_once(self.l,RESET_ID,T+1000*STEP,self.logs.append,self.market)
        self.assertEqual(self.l.s,before)
        self.assertEqual(json.loads(self.l.db.execute('SELECT payload FROM state').fetchone()[0]),before)
        self.assertEqual(counts,[self.l.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('events','marks')])
        self.assertFalse(any('reset_completed' in line for line in self.logs))

    def test_unknown_request_rejected_and_network_error_does_not_block_authorized_reset(self):
        with self.assertRaisesRegex(ValueError,'INVALID_INDICATOR_RESET_REQUEST'):
            reset_once(self.l,'typo',T,self.logs.append,self.market)
        self.market.get.side_effect=ValueError('INVALID_OHLCV')
        reset_once(self.l,RESET_ID,T+1000*STEP,self.logs.append,self.market)
        self.assertTrue(any('INVALID_OHLCV' in line for line in self.logs))
        self.assertFalse(self.l.pending())

    def test_fill_error_logs_candle_stage_and_specific_reason(self):
        market=Mock();market.prices.return_value={};market.fills.side_effect=ValueError('INVALID_OHLCV')
        paper=Mock(ledger=self.l)
        monitor=HourlyMonitor(self.root,self.logs.append,paper,clock=lambda:T+2*STEP)
        monitor.risk_cycle(market)
        self.assertTrue(any('fill_candles_5m' in x and 'INVALID_OHLCV' in x and 'KRW-A' in x for x in self.logs))


if __name__=='__main__':unittest.main()
