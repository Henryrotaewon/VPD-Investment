import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from datetime import datetime, timezone
from magi2.fast_rank_paper import RankLedger, rank_day, rank_slot, DAY, INTERVAL
from magi2.fast_rank_monitor import RankMarket, top_five, FastRankMonitor
from test_fast_tick import RULES, book

START=rank_day(1789970000000)+60000


def rows(*symbols):
    return [dict(symbol=s,asset=s,previous_close=90.,price=100.,rise_pct=100/90*100-100,rank=i)
            for i,s in enumerate(symbols,1)]


class RankTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'p.db'
        self.l=RankLedger(self.path,START)
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def snapshot(self, *symbols, ts=START):
        return self.l.save_rank('upbit',ts,rows(*symbols),ts,self.l.control()['generation'])
    def enter(self, symbol='X', ts=START):
        self.assertTrue(self.snapshot(symbol,ts=ts))
        ident=self.l.offer_rank('upbit',ts)[0]
        self.assertTrue(self.l.enter_market(ident,book(ts+300,99,100,100000),RULES,ts+300))
        return ident
    def test_schedule(self):
        self.assertEqual(rank_slot(START),START)
        self.assertEqual(rank_slot(START+299999),START)
        self.assertEqual(rank_slot(START+300000),START+300000)
        self.assertEqual(rank_day(START-60001),rank_day(START)-DAY)
    def test_no_order_before_snapshot_and_duplicate_guard(self):
        self.assertEqual(self.l.offer_rank('upbit',START),[])
        self.assertFalse(self.l.offer('legacy','upbit','X',START,START))
        self.assertTrue(self.snapshot('X'))
        self.assertFalse(self.snapshot('Y'))
        self.assertEqual(len(self.l.offer_rank('upbit',START)),1)
        self.assertEqual(self.l.offer_rank('upbit',START),[])
    def test_full_visible_depth_avoids_artificial_price_chase(self):
        self.snapshot('X');ident=self.l.offer_rank('upbit',START)[0]
        b=book(START+300,99,100,2000);b['asks']=[(100,2000),(120,100000)]
        self.l.enter_market(ident,b,RULES,START+300)
        self.assertAlmostEqual(self.l.get(ident)['buy_average'],100.05)
        self.assertLessEqual(self.l.get(ident)['entry_cost'],200000)
    def test_and_stop_only_on_new_fresh_rank(self):
        ident=self.enter()
        self.assertFalse(self.l.check_stop(ident,book(START+1000,90,100,100000),START+1000))
        ts=START+INTERVAL
        self.snapshot('Y',ts=ts)
        self.assertTrue(self.l.check_stop(ident,book(ts+100,90,100,100000),ts+100))
        self.assertEqual(self.l.get(ident)['reason'],'TOP5_EXIT_AND_STOP_6')
    def test_exit_rank_alone_does_not_sell_and_checks_once_per_scan(self):
        ident=self.enter();ts=START+INTERVAL;self.snapshot('Y',ts=ts)
        self.assertFalse(self.l.check_stop(ident,book(ts+100,99,100),ts+100))
        self.assertFalse(self.l.check_stop(ident,book(ts+200,90,100),ts+200))
        ts+=INTERVAL;self.snapshot('Y',ts=ts)
        self.assertTrue(self.l.check_stop(ident,book(ts+100,90,100),ts+100))
    def test_stale_failed_or_previous_day_rank_cannot_trigger_stop(self):
        ident=self.enter();ts=START+INTERVAL;self.snapshot('Y',ts=ts)
        self.assertFalse(self.l.check_stop(ident,book(ts+31000,90,100),ts+31000))
        self.assertFalse(self.l.check_stop(ident,book(START+DAY,90,100),START+DAY))
    def test_profit_without_tape_and_reentry_only_after_rank_return(self):
        ident=self.enter()
        self.assertTrue(self.l.check_stop(ident,book(START+1000,113,114,100000),START+1000))
        self.assertEqual(self.l.get(ident)['status'],'CLOSED')
        self.snapshot('X',ts=START+INTERVAL)
        self.assertEqual(self.l.offer_rank('upbit',START+INTERVAL),[])
        self.snapshot('Y',ts=START+2*INTERVAL)
        self.snapshot('X',ts=START+3*INTERVAL)
        self.assertEqual(len(self.l.offer_rank('upbit',START+3*INTERVAL)),1)
    def test_pause_race_and_resume_generation(self):
        self.l.pause_and_clear(START+1)
        self.assertFalse(self.snapshot('X',ts=START+INTERVAL))
        self.l.resume(START+2)
        self.assertFalse(self.l.save_rank('upbit',START+INTERVAL,rows('X'),START+INTERVAL,0))
        self.assertTrue(self.snapshot('X',ts=START+INTERVAL))
    def test_restart_preserves_cash_pause_baselines_and_rank_consumption(self):
        ident=self.enter();self.l.check_stop(ident,book(START+1000,113,114,100000),START+1000)
        self.l.save_baseline('upbit',rank_day(START),'X',90.)
        self.l.pause_and_clear(START+2000);cash=self.l.account('upbit')['cash_quote']
        self.l.close();self.l=RankLedger(self.path,START+DAY)
        self.assertFalse(self.l.control()['enabled'])
        self.assertEqual(self.l.account('upbit')['cash_quote'],cash)
        self.assertTrue(self.l.latest_rank('upbit')['rows'][0]['consumed'])
        self.assertEqual(self.l.baseline('upbit',rank_day(START),'X'),(True,90.))
    def test_slot_limit_and_available_cash(self):
        self.snapshot('A','B','C','D','E')
        ids=self.l.offer_rank('upbit',START);self.assertEqual(len(ids),5)
        self.assertLessEqual(sum(self.l.get(i)['budget_quote'] for i in ids),1000000)
        self.snapshot('F','B','C','D','E',ts=START+INTERVAL)
        self.assertEqual(self.l.offer_rank('upbit',START+INTERVAL),[])
        self.assertFalse(self.l.latest_rank('upbit')['rows'][0]['consumed'])

    def test_scan_commits_before_offer_and_rejects_incomplete_prices(self):
        paper=Mock();paper.ledger=self.l
        paper.wake={'upbit':Mock()}
        monitor=FastRankMonitor(Path(self.tmp.name),lambda _:None,paper,clock=lambda:START)
        market=Mock();market.symbols={'X':'X'};market.prices.return_value={'X':100.}
        self.assertTrue(monitor.scan('upbit',market,{'X':90.},START,0))
        self.assertEqual(len(self.l.active('upbit')),1)
        market.prices.return_value={}
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):
            monitor.scan('upbit',market,{'X':90.},START+INTERVAL,0)
        self.assertEqual(self.l.latest_rank('upbit')['slot'],START)

    def test_available_cash_under_slot_cap_after_loss(self):
        # Four live reservations leave only the actual remaining account cash.
        with self.l.lock,self.l.db:
            a=self.l._account('upbit');a['cash_quote']=850000.
            self.l.db.execute('UPDATE paper_accounts SET payload=? WHERE venue=?',(self.l._json(a),'upbit'))
        self.snapshot('A','B','C','D','E')
        ids=self.l.offer_rank('upbit',START)
        self.assertEqual([self.l.get(i)['budget_quote'] for i in ids],[200000]*4+[50000])


class AdapterTests(unittest.TestCase):
    def test_rank_is_calendar_close_not_rolling_or_open_and_deterministic(self):
        symbols={str(i):str(i) for i in range(7)}
        selected=top_five({s:100+int(s) for s in symbols},{s:100 for s in symbols},symbols)
        self.assertEqual([r['symbol'] for r in selected],['6','5','4','3','2'])
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):
            top_five({'X':1},{'X':1},{'X':'X','Y':'Y'})
    def test_four_venue_parsers_exclude_uncommitted_today(self):
        boundary=rank_day(START);previous=boundary-DAY
        stamp=datetime.fromtimestamp(previous/1000,timezone.utc).isoformat().replace('+00:00','')
        for venue in ('upbit','bithumb','binance','kraken'):
            market=RankMarket(venue)
            if venue in ('upbit','bithumb'):
                result=[dict(candle_date_time_utc=stamp,trade_price=95.)]
            elif venue=='binance':
                result=[[previous,0,0,0,95.,0,boundary-1],[boundary,0,0,0,120.,0,boundary+DAY-1]]
            else:result=dict(result={'X':[[previous//1000,0,0,0,95.],[boundary//1000,0,0,0,120.]],'last':1})
            market.get=Mock(return_value=result)
            self.assertEqual(market.previous_close('X',boundary),95.)
            market.http.close()
    def test_missing_previous_day_is_excluded_not_older_close(self):
        market=RankMarket('kraken');boundary=rank_day(START)
        market.get=Mock(return_value=dict(result={'X':[[boundary//1000,0,0,0,120.]],'last':1}))
        self.assertIsNone(market.previous_close('X',boundary));market.http.close()

if __name__=='__main__':unittest.main()
