import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from magi2 import point_in_time_scan as scan
from magi2 import closed_day_scan as closed
from magi2 import paper_engine as engine
from magi2 import server_runner as server

NOW = datetime.fromisoformat('2026-09-19T15:42:15+09:00')
ASOF = scan.cutoff_for(NOW)


def candle(start, price=100, value=10000):
    return {'candle_date_time_utc':start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
            'trade_price':price,'high_price':price+1,'low_price':price-1,
            'candle_acc_trade_volume':value/price,'candle_acc_trade_price':value}


def candles(cutoff=ASOF):
    start = scan.trading_day_start(cutoff)
    days = [candle(start-timedelta(days=i),100+i%6) for i in range(35,0,-1)]
    hour = cutoff.replace(minute=0)
    hours = [candle(hour-timedelta(hours=i),105,100) for i in range(26,0,-1)]
    minutes = [candle(hour+timedelta(minutes=i),106,1) for i in range(cutoff.minute)]
    return days,hours,minutes


def bundle(asof=ASOF):
    row={'coin':'X','market':'KRW-X','Rank':1,'VPD':50,'momentum':'→','TodayValue/10':.3,'IntraAccel':.4}
    return {'schema':scan.SCHEMA,'basis':'POINT_IN_TIME','asof':asof.isoformat(),
            'generated_at':asof.isoformat(),'top10':[row],'all_rows':{'X':row}}


class CandleTests(unittest.TestCase):
    def test_reconstructs_partial_day_without_double_counting(self):
        row,reason=scan.analyse('KRW-X',*candles(),ASOF)
        self.assertIsNone(reason)
        self.assertEqual(row['price'],106)
        self.assertAlmostEqual(row['TodayValue/10'],(6*100+42)/10000)
        self.assertEqual(row['signal_asof'],ASOF.isoformat())

    def test_future_and_open_candles_never_leak_into_score(self):
        days,hours,minutes=candles()
        expected,_=scan.analyse('KRW-X',days,hours,minutes,ASOF)
        days.append(candle(scan.trading_day_start(ASOF),1e8,1e12))
        hours.append(candle(ASOF.replace(minute=0),1e8,1e12))
        minutes.append(candle(ASOF,1e8,1e12))
        actual,_=scan.analyse('KRW-X',days,hours,minutes,ASOF)
        self.assertEqual(actual,expected)

    def test_all_hours_and_midnight_use_correct_trading_day(self):
        for h,m in [(0,0),(7,30),(8,45),(9,0),(9,1),(18,0),(23,59)]:
            cutoff=ASOF.replace(hour=h,minute=m)
            row,reason=scan.analyse('KRW-X',*candles(cutoff),cutoff)
            self.assertIsNone(reason,(h,m,reason))
            self.assertEqual(row['signal_asof'],cutoff.isoformat())
            start=scan.trading_day_start(cutoff)
            self.assertLessEqual(start,cutoff)
            self.assertLess(cutoff,start+timedelta(days=1))

    def test_nine_oclock_matches_previous_final_day_formula(self):
        cutoff=ASOF.replace(hour=9,minute=0)
        days,hours,minutes=candles(cutoff)
        expected,_=closed.analyse('KRW-X',days,hours,cutoff)
        actual,_=scan.analyse('KRW-X',days,hours,minutes,cutoff)
        for key,value in expected.items(): self.assertEqual(actual[key],value,key)
        self.assertEqual(actual['daily_basis'],'CLOSED_DAY')

    def test_no_trade_day_carries_price_with_zero_volume(self):
        days,_,_=candles()
        row,reason=scan.analyse('KRW-X',days,[],[],ASOF)
        self.assertIsNone(reason)
        self.assertEqual(row['price'],days[-1]['trade_price'])
        self.assertEqual(row['TodayValue/10'],0)
        self.assertEqual(row['IntraAccel'],0)
        self.assertIsNone(row['1H%'])

    def test_invalid_intraday_prices_fail_scan(self):
        days,hours,minutes=candles();minutes[-1]['trade_price']=float('nan')
        with self.assertRaises(ValueError):scan.analyse('KRW-X',days,hours,minutes,ASOF)


class BundleTests(unittest.TestCase):
    def client(self,fail=False):
        days,hours,minutes=candles()
        def get(path,params):
            if path=='/market/all':return [{'market':f'KRW-X{i}'} for i in range(10)]
            if fail and params['market']=='KRW-X5':raise TimeoutError()
            end={'/candles/days':scan.trading_day_start(ASOF),'/candles/minutes/60':ASOF.replace(minute=0),
                 '/candles/minutes/1':ASOF}[path]
            self.assertEqual(params['to'],end.astimezone(timezone.utc).isoformat())
            return {'/candles/days':days,'/candles/minutes/60':hours,'/candles/minutes/1':minutes}[path]
        return Mock(get=Mock(side_effect=get))

    def test_atomic_bundle_pins_all_ranks_and_same_minute_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vpd.json'
            snapshot=scan.build_snapshot(path,NOW,self.client(),lambda:NOW)
            self.assertEqual(len(snapshot['all_rows']),10)
            self.assertEqual(snapshot['top10'][0],snapshot['all_rows'][snapshot['top10'][0]['coin']])
            client=self.client()
            self.assertEqual(scan.build_snapshot(path,NOW,client,lambda:NOW),snapshot)
            client.get.assert_not_called()
            self.assertIsNone(scan.read_snapshot(path,NOW, (ASOF-timedelta(minutes=1)).isoformat()))
            self.assertIsNone(scan.read_snapshot(path,NOW+timedelta(minutes=11)))
            self.assertIsNone(scan.read_snapshot(path,ASOF-timedelta(seconds=1)))

    def test_failed_or_late_scan_never_publishes_partial_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vpd.json';path.write_text('old snapshot')
            with self.assertRaises(TimeoutError):scan.build_snapshot(path,NOW,self.client(True),lambda:NOW)
            self.assertEqual(path.read_text(),'old snapshot')
            with self.assertRaises(TimeoutError):scan.build_snapshot(path,NOW,self.client(),lambda:NOW+timedelta(minutes=11))
            self.assertEqual(path.read_text(),'old snapshot')

    def test_midnight_crossing_does_not_invalidate_fresh_data(self):
        asof=ASOF.replace(hour=23,minute=59)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vpd.json';path.write_text(json.dumps(bundle(asof)))
            self.assertIsNotNone(scan.read_snapshot(path,asof+timedelta(minutes=2)))


class EngineTests(unittest.TestCase):
    def test_only_current_request_bundle_is_used_without_remote_mix(self):
        snapshot=bundle()
        with patch.object(engine,'now_dt',return_value=NOW),patch.object(engine,'read_snapshot',return_value=snapshot) as read,patch.object(engine.requests,'get') as network:
            self.assertEqual(engine.load_morning_snapshot(ASOF.isoformat()),(snapshot,ASOF))
            self.assertEqual(read.call_args.args[2],ASOF.isoformat())
        network.assert_not_called()

    def test_all_hours_accepted_but_old_unmarked_and_future_snapshots_rejected(self):
        for hour in range(24):
            asof=ASOF.replace(hour=hour)
            self.assertIsNone(engine.snapshot_issue(asof,asof+timedelta(minutes=2),bundle(asof)))
        self.assertIsNotNone(engine.snapshot_issue(ASOF,NOW,{}))
        self.assertIsNotNone(engine.snapshot_issue(ASOF,NOW+timedelta(minutes=11),bundle()))
        self.assertIsNotNone(engine.snapshot_issue(ASOF,NOW-timedelta(minutes=1),bundle()))

    def test_same_day_reentry_blocked_and_duplicate_scan_idempotent(self):
        state={'cash_krw':100,'positions':{'X':{'status':'CLOSED','exit_at':(ASOF-timedelta(minutes=30)).isoformat()}}}
        with patch.object(engine,'now_dt',return_value=NOW),patch.object(engine,'load_morning_snapshot',return_value=(bundle(),ASOF)),patch.object(engine,'get_prices',return_value={'KRW-X':100}),patch.object(engine,'save_state'),patch.object(engine,'log_event'),patch.object(engine,'telegram'),patch.object(engine,'send_current_status'),patch.object(engine,'buy_position') as buy:
            self.assertTrue(engine.morning_rebalance(state))
            buy.assert_not_called()
            self.assertIn('재진입 유예',state['last_rebalance_decisions'][0])
            before=copy.deepcopy(state)
            self.assertFalse(engine.morning_rebalance(state))
            self.assertEqual(state,before)


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.patches=[patch.object(server,'SCAN_JOB',None),patch.object(server,'SCAN_CONTEXT',None),
                      patch.object(server,'ENGINE_JOB',None),patch.object(server,'ENGINE_MODE',None),
                      patch.object(server,'ENGINE_REQUEST_ID',None),patch.object(server,'telegram'),
                      patch.object(server,'EXECUTOR'),patch.object(server,'SCAN_EXECUTOR'),
                      patch.object(server,'read_vpd',return_value=bundle()),patch.object(server,'datetime')]
        mocks=[p.start() for p in self.patches]
        self.send,self.worker,self.scanworker,self.cache,self.dt=mocks[5:]
        self.dt.now.return_value=NOW
        self.future=Future();self.scanworker.submit.return_value=self.future
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def test_scan_does_not_block_monitor_and_repeated_request_is_coalesced(self):
        self.assertTrue(server.start_engine('morning'))
        self.assertFalse(server.start_engine('morning'))
        self.assertFalse(server.start_engine('refill'))
        self.assertTrue(server.start_engine('monitor'))
        self.worker.submit.assert_called_once_with(server.run_engine,'monitor')
        self.scanworker.submit.assert_called_once()

    def test_completed_scan_resumes_same_request_exactly_once(self):
        server.start_engine('morning');self.future.set_result(bundle())
        server.finish_scan_job();server.finish_scan_job()
        self.worker.submit.assert_called_once_with(server.run_engine,'morning',ASOF.isoformat())

    def test_full_rebuild_resumes_its_own_mode_and_blocks_other_rebalances(self):
        self.worker.submit.return_value=Future()
        server.start_engine('rebuild')
        self.assertFalse(server.start_engine('morning'))
        self.assertFalse(server.start_engine('rebuild'))
        self.future.set_result(bundle())
        server.finish_scan_job();server.finish_scan_job()
        self.worker.submit.assert_called_once_with(server.run_engine,'rebuild',ASOF.isoformat())
        self.assertFalse(server.start_engine('morning'))

    def test_scan_can_start_during_monitor_but_order_worker_waits(self):
        server.ENGINE_JOB=Future();server.ENGINE_MODE='monitor'
        self.assertTrue(server.start_engine('morning'))
        self.future.set_result(bundle());server.finish_scan_job()
        self.worker.submit.assert_not_called()
        server.ENGINE_JOB.set_result(None);server.finish_scan_job()
        self.worker.submit.assert_called_once_with(server.run_engine,'morning',ASOF.isoformat())

    def test_scan_failure_or_changed_bundle_never_trades(self):
        server.start_engine('morning');self.future.set_exception(TimeoutError())
        server.finish_scan_job();self.worker.submit.assert_not_called()
        self.future=Future();self.scanworker.submit.return_value=self.future
        server.start_engine('morning');self.future.set_result(bundle(ASOF-timedelta(minutes=1)))
        server.finish_scan_job();self.worker.submit.assert_not_called()


class OneShotTests(unittest.TestCase):
    def test_authorized_request_consumed_once_even_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            request={'id':'user_requested_1','expires_at':(NOW+timedelta(hours=1)).isoformat()}
            with patch.dict(os.environ,{'MAGI2_REBALANCE_ONCE':json.dumps(request)}),patch.object(server,'STATE_DIR',Path(directory)),patch.object(server,'datetime') as clock,patch.object(server,'start_engine',return_value=True) as start:
                clock.now.return_value=NOW
                clock.fromisoformat.side_effect=datetime.fromisoformat
                server.consume_startup_rebalance();server.consume_startup_rebalance()
                start.assert_called_once_with('morning',request_id='user_requested_1')
                self.assertEqual(json.loads((Path(directory)/'rebalance_requests.json').read_text())[request['id']]['status'],'claimed')

    def test_expired_or_malformed_requests_never_execute(self):
        with patch.object(server,'start_engine') as start,patch.object(server,'datetime') as clock:
            clock.now.return_value=NOW
            clock.fromisoformat.side_effect=datetime.fromisoformat
            for raw in ['{}',json.dumps({'id':'x','expires_at':NOW.isoformat()}),json.dumps({'id':'x','expires_at':'2026-09-19T18:00:00'})]:
                with patch.dict(os.environ,{'MAGI2_REBALANCE_ONCE':raw}):server.consume_startup_rebalance()
            start.assert_not_called()


if __name__=='__main__':unittest.main()
