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



if __name__=='__main__':unittest.main()
