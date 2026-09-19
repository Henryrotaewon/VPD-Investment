import copy
import json
import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from magi2 import closed_day_scan as scan
from magi2 import paper_engine as engine
from magi2 import server_runner as server

NOW=datetime.fromisoformat('2026-09-19T09:15:00+09:00')
CUTOFF=scan.cutoff_for(NOW)


def candles():
    daily=[]
    for offset in range(35,0,-1):
        start=CUTOFF-timedelta(days=offset)
        price=100+(35-offset)%6
        daily.append({'candle_date_time_utc':start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
                      'trade_price':price,'high_price':price+1,'low_price':price-1,
                      'candle_acc_trade_volume':100+offset,'candle_acc_trade_price':10000+offset})
    hourly=[{'candle_date_time_utc':(CUTOFF-timedelta(hours=h)).astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
             'trade_price':100 if h==2 else 101} for h in (2,1)]
    return daily,hourly


class CandleTests(unittest.TestCase):
    def test_post_close_daily_and_hourly_data_never_leak_into_score(self):
        days,hours=candles()
        expected,_=scan.analyse('KRW-X',days,hours,CUTOFF)
        new=dict(days[-1],candle_date_time_utc=CUTOFF.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
                 trade_price=1e9,high_price=1e9,candle_acc_trade_price=1e12)
        actual,_=scan.analyse('KRW-X',days+[new],hours+[new],CUTOFF)
        self.assertEqual(actual,expected)
        self.assertAlmostEqual(actual['1H%'],1)

    def test_missing_new_candle_does_not_drop_real_previous_day(self):
        days,hours=candles()
        row,reason=scan.analyse('KRW-X',list(reversed(days)),hours,CUTOFF)
        self.assertIsNone(reason)
        self.assertEqual(row['price'],days[-1]['trade_price'])

    def test_short_history_stale_day_and_duplicate_are_explicit(self):
        days,hours=candles()
        self.assertEqual(scan.analyse('KRW-X',days[:5],hours,CUTOFF)[1],'SHORT_HISTORY')
        self.assertEqual(scan.analyse('KRW-X',days[:-1],hours,CUTOFF)[1],'NO_CLOSED_DAY')
        with self.assertRaises(ValueError):scan.analyse('KRW-X',days+[days[-1]],hours,CUTOFF)

    def test_nonconsecutive_hourly_candles_are_not_called_one_hour_return(self):
        days,hours=candles()
        hours[0]['candle_date_time_utc']=(CUTOFF-timedelta(hours=3)).astimezone(timezone.utc).replace(tzinfo=None).isoformat()
        row,_=scan.analyse('KRW-X',days,hours,CUTOFF)
        self.assertIsNone(row['1H%'])

    def test_formula_loading_does_not_run_scanner_network_or_publication(self):
        scan.formulas.cache_clear()
        with patch.object(scan.requests,'get',side_effect=AssertionError('network')),patch('builtins.input',side_effect=AssertionError('prompt')):
            score=scan.formulas()['score_vpd'](3,3,4,50,-60,0,False)
        self.assertEqual(score,100)


class SnapshotTests(unittest.TestCase):
    def client(self,fail=False):
        days,hours=candles()
        def get(path,params):
            if path=='/market/all':return [{'market':f'KRW-X{i}'} for i in range(10)]
            self.assertEqual(params['to'],CUTOFF.astimezone(timezone.utc).isoformat())
            if fail and params['market']=='KRW-X5':raise TimeoutError('read failure')
            return days if path=='/candles/days' else hours
        return Mock(get=Mock(side_effect=get))

    def test_single_bundle_cache_and_no_side_effect_before_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vpd_closed_day.json'
            c=self.client()
            with self.assertRaises(ValueError):scan.build_snapshot(path,CUTOFF-timedelta(seconds=1),c)
            c.get.assert_not_called()
            snap=scan.build_snapshot(path,NOW,c,lambda:NOW)
            self.assertEqual(snap['asof'],CUTOFF.isoformat())
            self.assertEqual(len(snap['all_rows']),10)
            self.assertIsNotNone(scan.read_snapshot(path,NOW))
            self.assertIsNone(scan.read_snapshot(path,NOW+timedelta(days=1)))
            self.assertIsNone(scan.read_snapshot(path,CUTOFF))

    def test_failure_does_not_publish_partial_data_or_overwrite_old_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'vpd_closed_day.json';path.write_text('previous')
            with self.assertRaises(TimeoutError):scan.build_snapshot(path,NOW,self.client(True),lambda:NOW)
            self.assertEqual(path.read_text(),'previous')


class ClosedEngineTests(unittest.TestCase):
    def test_closed_phase_does_not_rebuy_a_coin_sold_earlier_today(self):
        row={'coin':'X','market':'KRW-X','Rank':1,'VPD':90,'momentum':'↑','TodayValue/10':2,'IntraAccel':2}
        state={'cash_krw':100,'positions':{'X':{'status':'CLOSED','exit_at':(CUTOFF-timedelta(minutes=30)).isoformat()}}}
        snap={'schema':scan.SCHEMA,'asof':CUTOFF.isoformat(),'generated_at':NOW.isoformat(),'top10':[row],'all_rows':{'X':row}}
        with patch.object(engine,'now_dt',return_value=NOW),patch.object(engine,'load_morning_snapshot',return_value=(snap,CUTOFF)),patch.object(engine,'get_prices',return_value={'KRW-X':100}),patch.object(engine,'save_state'),patch.object(engine,'log_event'),patch.object(engine,'telegram'),patch.object(engine,'send_current_status'),patch.object(engine,'buy_position') as buy:
            engine.morning_rebalance(state)
        buy.assert_not_called()
        self.assertIn('재진입 유예',state['last_rebalance_decisions'][0])

    def test_post_nine_requires_explicit_final_candle_metadata(self):
        cfg=copy.deepcopy(engine.CFG)
        with patch.object(engine,'CFG',cfg):
            self.assertIsNotNone(engine.snapshot_issue(CUTOFF,NOW,{}))
            snap={'schema':scan.SCHEMA,'generated_at':NOW.isoformat()}
            self.assertIsNone(engine.snapshot_issue(CUTOFF,NOW,snap))
            self.assertIsNotNone(engine.snapshot_issue(CUTOFF-timedelta(days=1),NOW,snap))
            self.assertIsNotNone(engine.snapshot_issue(CUTOFF,NOW+timedelta(days=1),snap))

    def test_closed_bundle_used_without_fetching_latest_partial_day(self):
        snap={'schema':scan.SCHEMA,'asof':CUTOFF.isoformat()}
        with patch.object(engine,'now_dt',return_value=NOW),patch.object(engine,'read_snapshot',return_value=snap),patch.object(engine.requests,'get') as network:
            self.assertEqual(engine.load_morning_snapshot(),(snap,CUTOFF))
        network.assert_not_called()

    def test_two_phase_weakening_preserves_time_grace_and_completed_day_is_idempotent(self):
        cfg=copy.deepcopy(engine.CFG);cfg['session']['top_n']=1;cfg['fee_rate']=0
        row={'coin':'X','market':'KRW-X','Rank':1,'VPD':50,'momentum':'→','TodayValue/10':.3,'IntraAccel':.4}
        first=CUTOFF-timedelta(minutes=98)
        state={'cash_krw':0,'initial_cash_krw':100,'last_rebalance_date':NOW.date().isoformat(),
               'last_rebalance_vpd_asof':first.isoformat(),'positions':{'X':{'status':'OPEN','market':'KRW-X',
               'qty':1,'cost_krw':100,'entry_price':100,'peak_price':101,'weakening_count':1,
               'weakening_policy':engine.POLICY,'weakening_since':first.isoformat()}}}
        snap={'schema':scan.SCHEMA,'asof':CUTOFF.isoformat(),'generated_at':NOW.isoformat(),'top10':[row],'all_rows':{'X':row}}
        with patch.object(engine,'CFG',cfg),patch.object(engine,'now_dt',return_value=NOW),patch.object(engine,'load_morning_snapshot',return_value=(snap,CUTOFF)),patch.object(engine,'load_all_ranked_rows') as remote,patch.object(engine,'get_prices',return_value={'KRW-X':101}),patch.object(engine,'save_state'),patch.object(engine,'log_event'),patch.object(engine,'telegram'),patch.object(engine,'send_current_status'):
            self.assertTrue(engine.morning_rebalance(state))
            self.assertEqual(state['positions']['X']['status'],'OPEN')
            self.assertEqual(state['positions']['X']['weakening_count'],2)
            before=copy.deepcopy(state)
            self.assertFalse(engine.morning_rebalance(state))
            self.assertEqual(state,before)
        remote.assert_not_called()


class BackgroundScanTests(unittest.TestCase):
    def setUp(self):
        self.patches=[patch.object(server,'CLOSED_JOB',None),patch.object(server,'ENGINE_JOB',None),
                      patch.object(server,'telegram'),patch.object(server,'finish_engine_job'),
                      patch.object(server,'EXECUTOR'),patch.object(server,'CLOSED_EXECUTOR'),
                      patch.object(server,'read_closed_day',return_value=None),patch.object(server,'datetime')]
        mocks=[p.start() for p in self.patches]
        self.send,self.finish,self.worker,self.scanworker,self.cache,self.dt=mocks[2:]
        self.dt.now.return_value=NOW
        self.future=Future();self.scanworker.submit.return_value=self.future
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def test_scan_does_not_occupy_paper_worker_or_block_monitor(self):
        self.assertTrue(server.start_engine('morning'))
        self.scanworker.submit.assert_called_once()
        self.worker.submit.assert_not_called()
        self.assertFalse(server.start_engine('morning'))
        self.assertTrue(server.start_engine('monitor'))
        self.worker.submit.assert_called_once_with(server.run_engine,'monitor')

    def test_completed_scan_resumes_authorized_request_exactly_once(self):
        server.start_engine('morning');self.future.set_result({})
        self.cache.return_value={'asof':CUTOFF.isoformat()}
        server.finish_closed_day_job();server.finish_closed_day_job()
        self.worker.submit.assert_called_once_with(server.run_engine,'morning')

    def test_failed_or_late_scan_never_starts_trading(self):
        server.start_engine('morning');self.future.set_exception(TimeoutError())
        server.finish_closed_day_job();self.worker.submit.assert_not_called()
        self.future=Future();self.scanworker.submit.return_value=self.future
        server.start_engine('morning');self.future.set_result({})
        self.dt.now.return_value=NOW+timedelta(days=1)
        server.finish_closed_day_job();self.worker.submit.assert_not_called()


if __name__=='__main__':unittest.main()
