import json
import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import Mock
from magi2.indicator_paper import IndicatorLedger, IndicatorPaperService, COHORT
from magi2.indicator_monitor import IndicatorMonitor, LiquidIndicatorMarket
from magi2.fast_wave_indicator import evaluate, KEYS, VERSION
from magi2.fast_wave_store import EvidenceStore
from magi2.fast_paper_report import positions_page, menu
from magi2.strategy_guide import strategy_text
from magi2.telegram_ui import validation_text

T=1790295120000

def decision(ts=T, venue='upbit', symbol='KRW-T'):
    base=dict(zip(KEYS,(0.,50.,-60.,1.)));points=[]
    for i,n in enumerate((0.,1.,3.)):
        points.append(dict(strategy_version=VERSION,venue=venue,symbol=symbol,day_ms=ts//86400000*86400000,
                           baseline_ms=ts//86400000*86400000-86400000,baseline=base.copy(),
                           observed_ms=ts-(2-i)*60000,current_volume=100+i,
                           current={k:base[k]+n*s for k,s in zip(KEYS,(1,1,2,.1))}))
    flow=dict(observed_ms=ts,sufficient=True,sample_trades=30,sample_span_ms=6000,
              latest_trade_age_ms=100,buyer_share_pct=75,spread_bps=5)
    return evaluate(points,ts,flow=flow)

def book(ts,bid=100,ask=100.1):
    return dict(requested_ms=ts-1,received_ms=ts,bids=[(bid,1e9)],asks=[(ask,1e9)])

RULES=dict(step='0.00000001',tick='.1',min_qty=0,min_notional=1,source='TEST')

class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'paper.db'
        self.l=IndicatorLedger(self.path,T-180000)
    def tearDown(self):self.l.close();self.temp.cleanup()
    def enter(self,ts=T,venue='upbit',symbol='KRW-T'):
        self.assertTrue(self.l.offer_decision(decision(ts,venue,symbol),100,ts,ts,self.l.control()['generation']))
        ident=self.l.active(venue)[-1]['id']
        self.assertTrue(self.l.enter_market(ident,book(ts+500),RULES,ts+500))
        return ident
    def test_four_venues_funding_slots_evidence_and_no_legacy_read(self):
        for venue in ('binance','kraken'):self.l.fund(venue,1400,'test',T)
        for venue in ('upbit','bithumb','binance','kraken'):
            for i in range(5):
                self.assertTrue(self.l.offer_decision(decision(T,venue,str(i)),100,T,T,0))
            self.assertFalse(self.l.offer_decision(decision(T,venue,'six'),100,T,T,0))
            self.assertEqual(len(self.l.active(venue)),5)
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM indicator_signals').fetchone()[0],24)
        self.assertFalse(self.l.offer('legacy','upbit','OLD',T,T))
    def test_pause_stale_flow_duplicate_and_generation(self):
        r=decision();self.assertTrue(self.l.offer_decision(r,100,T,T,0))
        self.assertFalse(self.l.offer_decision(r,100,T,T,0))
        self.l.pause_and_clear(T+10)
        self.assertFalse(self.l.offer_decision(decision(T+20),100,T+20,T+20,0))
        self.l.resume(T+30)
        self.assertFalse(self.l.offer_decision(decision(T+40),100,T+40,T+40,0))
        self.assertFalse(self.l.offer_decision(decision(T+40),100,T,T+4000,self.l.control()['generation']))
    def test_hard_stop_is_independent_and_cash_conserved(self):
        ident=self.enter();t=self.l.get(ident)
        self.assertTrue(self.l.check_stop(ident,book(T+2000,93,93.1),T+2000))
        self.assertTrue(self.l.exit(ident,book(T+2100,93,93.1),T+2100))
        t=self.l.get(ident);self.assertEqual(t['status'],'CLOSED');self.assertEqual(t['reason'],'STOP_LOSS_6')
        self.assertAlmostEqual(self.l.account('upbit')['cash_quote'],1000000+t['realized_quote'])
        self.assertFalse(self.l.offer_decision(decision(T+60000),100,T+60000,T+60000,0))
        self.assertIn('SOLD_TODAY_KST',self.l.db.execute('SELECT payload FROM paper_trades ORDER BY signal_ms DESC LIMIT 1').fetchone()[0])
    def test_profit_protection_and_restart_preserve_peak(self):
        ident=self.enter();self.l.check_stop(ident,book(T+2000,108,108.1),T+2000)
        self.l.close();self.l=IndicatorLedger(self.path,T+3000)
        self.assertGreater(self.l.get(ident)['peak_net_pct'],6)
        self.assertTrue(self.l.check_stop(ident,book(T+4000,105,105.1),T+4000))
        self.assertEqual(self.l.get(ident)['reason'],'PROFIT_PROTECTION')
    def test_take_profit_and_time_exit(self):
        ident=self.enter();self.l.check_stop(ident,book(T+2000,114,114.1),T+2000)
        self.assertEqual(self.l.get(ident)['reason'],'TAKE_PROFIT_12')
        other=self.enter(T+10000,'upbit','KRW-OTHER')
        self.l.check_stop(other,book(T+3611000),T+3611000)
        self.assertEqual(self.l.get(other)['reason'],'MAX_HOLD_60M')
    def test_weakness_requires_two_distinct_fresh_observations(self):
        ident=self.enter()
        def weak(ts):
            return dict(ready=True,score=0,latest=dict(observed_ms=ts),
                        velocity_per_minute=dict(macd_hist_bps=-1,rsi=-1))
        self.l.observe_holding('upbit','KRW-T',weak(T+60000),T+60000)
        self.l.observe_holding('upbit','KRW-T',weak(T+60000),T+60000)
        self.assertEqual(self.l.get(ident)['status'],'OPEN')
        self.l.observe_holding('upbit','KRW-T',weak(T+120000),T+120000)
        self.assertEqual(self.l.get(ident)['reason'],'INDICATOR_WEAKENED')
    def test_marks_and_daily_are_historical_not_current(self):
        for venue in ('binance','kraken'):self.l.fund(venue,1400,'test',T)
        self.l.record_mark(T);self.l.record_mark(T+100)
        date=self.l.report_day(T)
        saved=self.l.daily_text(date,T)
        ident=self.enter();self.l.check_stop(ident,book(T+1000,90,90.1),T+1000)
        self.l.exit(ident,book(T+1100,90,90.1),T+1100)
        self.assertIn('4,000,000원',self.l.daily_text(date,T+1200))
        self.assertEqual(self.l.performance()['samples'],1)
        self.assertIn('마감 직전 평가 누락',saved)
        self.assertIn('MACD',menu(self.l)[0]);self.assertNotIn('TOP 5 이탈',positions_page(self.l,T+1200)[0])
    def test_separate_service_root_and_legacy_file_preserved(self):
        root=Path(self.temp.name);legacy=root/'fast_paper.sqlite3';legacy.write_bytes(b'old')
        s=IndicatorPaperService(root,lambda _:None,clock=lambda:T)
        self.assertEqual(s.ledger.db.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0],0)
        self.assertEqual(legacy.read_bytes(),b'old');s.ledger.close()

class MonitorTests(unittest.TestCase):
    def test_three_samples_to_persisted_decision_to_entry_and_holding(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=IndicatorLedger(Path(root)/'paper.db',T-180000)
            store=EvidenceStore(root,'test');r=decision()
            for p in r['observations'][:2]:store.append(p)
            market=Mock();market.venue='upbit';market.observe.return_value=r['latest']
            market.capture.return_value=(100,T);market.flow.return_value=r['flow']
            paper=Mock();paper.ledger=ledger;paper.wake={v:Mock() for v in ('upbit','bithumb','binance','kraken')}
            monitor=IndicatorMonitor(root,lambda _:None,paper,clock=lambda:T)
            monitor.stop=Mock();monitor.stop.is_set.return_value=False
            monitor.scan(market,store,['KRW-T'],0)
            self.assertEqual(len(ledger.active('upbit')),1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0],1)
            self.assertIn('100/100',monitor.captures()[0])
            store.close();ledger.close()
    def test_liquidity_selection_is_not_price_gainers(self):
        market=LiquidIndicatorMarket('binance');market.symbols={'A':'A','B':'B'}
        market.get=lambda *args:[dict(symbol='A',quoteVolume='100',priceChangePercent='90'),
                                 dict(symbol='B',quoteVolume='900',priceChangePercent='-1')]
        self.assertEqual(market.liquid_symbols(1),['B']);market.http.close()
    def test_legacy_wave_and_new_report_routes_are_read_only(self):
        from unittest.mock import patch
        from magi2 import server_runner as server
        callback={'id':'test','data':'guide:wave','from':{'id':'7'},'message':{'chat':{'id':'7'}}}
        with patch.object(server,'ALLOWED_CHAT_ID','7'), patch.object(server,'telegram') as send, \
             patch.object(server,'telegram_api'),patch.object(server,'start_engine') as execute:
            server.handle_callback(callback)
            self.assertIn('MACD',send.call_args.args[0]);execute.assert_not_called()
            callback['message']['chat']['id']='8'
            server.handle_callback(callback);self.assertEqual(send.call_count,1)

    def test_guide_matches_policy(self):
        for term in ('MACD','RSI','Williams','60분','70%','65','2%p'):
            self.assertIn(term,strategy_text('wave'))
        self.assertNotIn('시작과 확산',validation_text())

if __name__=='__main__':unittest.main()
