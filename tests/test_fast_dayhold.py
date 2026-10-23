import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from magi1.fast_dayhold import DayHoldPaper, btc_state, early_evidence, RISK, FIVE, DAY
from magi1.fast_paper import Paper, POLICY
from magi2.paper_dashboard import guide, target

T=20000*DAY


def book(ts, bid=100, size=100000):
    return dict(ts=ts,stamp=ts,bp=bid,ap=bid+.01,bids=[(bid,size)],asks=[(bid+.01,size)])


class DayHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'paper.db'
        self.p=DayHoldPaper(self.path,T)
        self.p.set_regime(dict(state='NEUTRAL',ts=T))
    def tearDown(self):
        self.p.db.close();self.tmp.cleanup()
    def buy(self):
        self.p.signal('KRW-X',T+1,100,98,dict(first_condition_price=100))
        self.p.on_trade('KRW-X',T+2,100,1,T+2)
        self.p.on_book('KRW-X',T+3,book(T+3))
        return self.p.s['positions']['KRW-X']
    def quote_for_return(self,p,ret,ts,size=100000):
        bid=p['original_cost']/p['original_qty']*(1+ret)/((1-self.p.policy['fee'])*(1-self.p.policy['slip']))
        return book(ts,bid,size)
    def test_short_noise_and_three_minutes_do_not_sell(self):
        p=self.buy(); p['value']=1000;p['volume']=10
        bars=[]
        for i in range(1,25):
            t=T+i*10000
            bars.append(dict(t=t,value=100,buy=1,c=99.9-i*.01,l=99.5))
            self.p.window('KRW-X',t+10000,bars)
        self.p.on_book('KRW-X',T+250001,book(T+250001,99.5))
        self.assertIsNone(p['exit']);self.assertIsNone(p['warning'])
        self.assertAlmostEqual(p['risk']['sl'],.04)
        self.assertLess(p['stop'],97)
    def test_take_profit_is_cost_adjusted_and_fills_on_next_book(self):
        p=self.buy()
        self.p.on_book('KRW-X',T+10,self.quote_for_return(p,.119,T+10))
        self.assertIsNone(p['exit'])
        self.p.on_book('KRW-X',T+11,self.quote_for_return(p,.121,T+11))
        self.assertEqual(p['exit']['reason'],'DAYHOLD_TAKE_PROFIT')
        self.assertEqual(self.p.s['closed'],0)
        self.p.on_book('KRW-X',T+12,self.quote_for_return(p,.121,T+12))
        self.assertEqual(self.p.s['closed'],1)
        self.assertGreater(self.p.s['realized'],p['original_cost']*.12)
        self.assertFalse(self.p.can_signal('KRW-X',T+20))
    def test_hard_sl_can_skip_warning_and_sell_partial_depth(self):
        p=self.buy();q=p['qty']
        self.p.on_book('KRW-X',T+10,self.quote_for_return(p,-.05,T+10))
        self.assertEqual(p['exit']['reason'],'DAYHOLD_HARD_SL')
        self.p.on_book('KRW-X',T+11,book(T+11,94,size=q/2))
        self.assertAlmostEqual(p['qty'],q/2)
        self.p.on_book('KRW-X',T+12,book(T+11,94,size=q/2))
        self.assertAlmostEqual(p['qty'],q/2)
        self.p.on_book('KRW-X',T+13,book(T+13,93))
        self.assertEqual(self.p.s['closed'],1)
        self.assertLess(self.p.s['realized'],-p['original_cost']*.04)
    def test_warning_recovery_and_pre_sl_failure(self):
        p=self.buy()
        self.p.on_book('KRW-X',T+100,self.quote_for_return(p,-.031,T+100))
        self.assertIsNotNone(p['warning']);self.assertIsNone(p['exit'])
        self.p.on_book('KRW-X',T+200,self.quote_for_return(p,-.02,T+200))
        self.assertIsNone(p['warning'])
        self.p.on_book('KRW-X',T+300,self.quote_for_return(p,-.031,T+300))
        p.update(flow_weak=True,flow_ts=T+20300)
        self.p.on_book('KRW-X',T+20301,self.quote_for_return(p,-.032,T+20301))
        self.assertEqual(p['exit']['reason'],'DAYHOLD_SL_APPROACH_FAILED_RECOVERY')
    def test_warning_does_not_sell_on_stale_flow(self):
        p=self.buy()
        self.p.on_book('KRW-X',T+100,self.quote_for_return(p,-.031,T+100))
        p.update(flow_weak=True,flow_ts=T+100)
        self.p.on_book('KRW-X',T+30000,self.quote_for_return(p,-.032,T+30000))
        self.assertIsNone(p['exit'])
    def test_sl_only_tightens_when_btc_recovers(self):
        p=self.buy();old=p['stop']
        self.p.set_regime(dict(state='DOWN',ts=T+100))
        tightened=p['stop'];self.assertGreater(tightened,old)
        self.assertEqual(p['risk']['tp'],.08)
        self.p.set_regime(dict(state='UP',ts=T+200))
        self.assertEqual(p['stop'],tightened);self.assertEqual(p['risk']['sl'],.03)
        self.assertEqual(p['risk']['tp'],.15)
    def test_stale_btc_or_shock_blocks_new_entry_not_holding(self):
        p=self.buy()
        self.p.signal('KRW-Y',T+180001,100,98,{})
        self.assertNotIn('KRW-Y',self.p.s['pending'])
        self.p.set_regime(dict(state='SHOCK',ts=T+180010))
        self.p.signal('KRW-Z',T+180011,100,98,{})
        self.assertNotIn('KRW-Z',self.p.s['pending']);self.assertEqual(p['risk']['sl'],.02)
    def test_first_condition_cap_rejects_iris_style_chase(self):
        self.p.signal('KRW-IRYS',T+1,33.7,31.7,dict(first_condition_price=33.2))
        self.p.on_trade('KRW-IRYS',T+2,33.8,100,T+2)
        self.p.on_book('KRW-IRYS',T+3,book(T+3,33.79))
        self.assertNotIn('KRW-IRYS',self.p.s['positions'])
        self.assertEqual(self.p.s['pending']['KRW-IRYS']['last_reason'],'ENTRY_PRICE_CAP')
    def test_24h_from_actual_buy_even_after_restart(self):
        p=self.buy();entry=p['entry_ms'];self.p.save(T+4);self.p.db.close()
        self.p=DayHoldPaper(self.path,T+1000)
        p=self.p.s['positions']['KRW-X'];self.assertIsNone(p['exit']);self.assertTrue(p['uncertain'])
        self.p.on_book('KRW-X',T+1001,book(T+1001))
        self.assertFalse(p['uncertain']);self.assertIsNone(p['exit'])
        self.p.tick(entry+DAY-1);self.assertIsNone(p['exit'])
        self.p.tick(entry+DAY);self.assertEqual(p['exit']['reason'],'DAYHOLD_24H_LIMIT')
        self.p.on_book('KRW-X',entry+DAY+1,book(entry+DAY+1))
        self.assertEqual(self.p.s['closed'],1)
    def test_gap_revalidates_hard_sl_and_never_fills_stale_book(self):
        p=self.buy();self.p.gap(['KRW-X'],T+100,'STALE_FEED')
        self.assertIsNone(p['exit'])
        self.p.on_book('KRW-X',T+5000,book(T+100,90))
        self.assertIsNone(p['exit'])
        self.p.on_book('KRW-X',T+5001,book(T+5001,90))
        self.assertEqual(p['exit']['reason'],'DAYHOLD_HARD_SL')
    def test_early_evidence_observation_has_no_order_and_is_bounded(self):
        f=dict(confirmation_delay_ms=0,return_10s_bps=20,price_acceleration_bps=10,
               turnover_change_ratio=1.5,buy_share_change=.1,ofi_change=3,
               rise_from_60s_low_bps=100,spread_bps=5)
        self.assertTrue(early_evidence(f)['eligible'])
        self.assertFalse(early_evidence(dict(f,price_acceleration_bps=-2))['eligible'])
        self.p.observe_early('KRW-X',T+1,f);self.p.observe_early('KRW-X',T+100,f)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM early_candidates').fetchone()[0],1)
        self.assertFalse(self.p.s['pending']);self.assertFalse(self.p.s['positions'])
    def test_reports_show_risk_and_warning(self):
        p=self.buy();p['warning']={'ts':T+10}
        self.assertIn('−4%',target('fast',p));self.assertIn('매도 준비',target('fast',p))
        self.assertIn('24시간',guide('fast'))


class MigrationTests(unittest.TestCase):
    def test_ledger_pause_history_and_tighter_legacy_stop_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'p.db';old=Paper(path,T)
            old.signal('KRW-X',T+1,100,99,{})
            old.on_trade('KRW-X',T+2,100,1,T+2);old.on_book('KRW-X',T+3,book(T+3))
            old.pause_entries(T+4);before=json.loads(json.dumps(old.s))
            fills=old.db.execute('SELECT * FROM fills').fetchall();old.db.close()
            p=DayHoldPaper(path,T+10)
            self.assertEqual(p.db.execute('SELECT * FROM fills').fetchall(),fills)
            for k in ('cash','realized','started_ms','closed','winning','entries_paused'):
                self.assertEqual(p.s[k],before[k])
            self.assertGreaterEqual(p.s['positions']['KRW-X']['stop'],99)
            self.assertIsNone(p.s['positions']['KRW-X']['exit'])
            self.assertEqual(len(p.s['policy_history']),1)
            p.db.close();p=DayHoldPaper(path,T+20)
            self.assertEqual(len(p.s['policy_history']),1);p.db.close()
    def test_unrelated_policy_migration_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'p.db';p=Paper(path,T,dict(POLICY,version='other'));p.db.close()
            with self.assertRaisesRegex(ValueError,'UNSUPPORTED_MIGRATION'):DayHoldPaper(path,T+1)


class RegimeTests(unittest.TestCase):
    def candles(self,trend):
        return [dict(candle_date_time_utc=datetime.fromtimestamp((T-(25-i)*FIVE)/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),trade_price=100*(1+trend)**i) for i in range(25)]
    def test_regime_classification_and_partial_bar_exclusion(self):
        for trend,state in [(0,'NEUTRAL'),(.001,'UP'),(-.001,'DOWN'),(-.004,'SHOCK')]:
            rows=self.candles(trend)
            rows.append(dict(candle_date_time_utc=datetime.fromtimestamp(T/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),trade_price=1))
            self.assertEqual(btc_state(rows,T)['state'],state)
    def test_missing_duplicate_stale_invalid_btc_rejected(self):
        rows=self.candles(0)
        for data,ts in [(rows[:-1],T),(rows[:-1]+[rows[-2]],T),(rows,T+FIVE),(rows[:-1]+[dict(rows[-1],trade_price=float('nan'))],T)]:
            with self.assertRaises(ValueError):btc_state(data,ts)


if __name__=='__main__':unittest.main()
