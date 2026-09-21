import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from magi2.fast_paper import PaperLedger, DEADLINE_MS
from magi2.fast_tick_paper import TickLedger, VERSION, validate_tape
from magi2.fast_tick_rules import UPBIT_GRID, BITHUMB_GRID, adjacent
from magi2.fast_tick_service import TickPaperService, TickMarket
from magi2.fast_paper_report import recent, summary, history, return_pct

START=1789970000000
RULES=dict(tick='1',step='.001',min_qty=.001,min_notional=5)


def trade(ident,ts,price=100,qty=1000,buyer=False):
    return dict(id=str(ident),ts=ts,price=price,qty=qty,buyer=buyer)


def tape(ts,*rows):
    return dict(requested_ms=ts-20,received_ms=ts,trades=list(rows) or [trade('initial',START-100)])


def book(ts,bid=99,ask=101,size=1000):
    return dict(requested_ms=ts-20,received_ms=ts,
                bids=[(bid,size),(bid-1,size),(bid-2,size)],
                asks=[(ask,size),(ask+1,size),(ask+2,size)])


class GridTests(unittest.TestCase):
    def test_direction_and_band_crossings(self):
        u=dict(grid=UPBIT_GRID);b=dict(grid=BITHUMB_GRID)
        for p,down,up in [(100,99.9,101),(1000,999,1001),(5000,4999,5005),(1000000,999500,1001000)]:
            self.assertEqual(adjacent(p,'BUY',u),down)
            self.assertEqual(adjacent(p,'SELL',u),up)
        self.assertEqual(adjacent(100,'BUY',b),99.99)
        self.assertEqual(adjacent(.02,'SELL',b),.0201)
        self.assertEqual(adjacent(100,'BUY',RULES),99)

    def test_bad_and_future_tape_rejected(self):
        for data in [tape(START,trade('1',START),trade('1',START)),
                     tape(START,trade('1',START+1001)),
                     tape(START,trade('1',START,qty=-1))]:
            with self.assertRaises(ValueError):validate_tape(data,START)


class TickTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'paper.db'
        self.l=TickLedger(self.path,START)
        self.assertTrue(self.l.offer('s','upbit','KRW-T',START,START))

    def tearDown(self):
        self.l.close();self.tmp.cleanup()

    def step(self,ts,rows=None,b=None):
        return self.l.limit_step('s',tape(ts,*(rows or [])),b,RULES,ts)

    def activate_buy(self,queue=1000):
        self.step(START)
        self.assertEqual(self.l.get('s')['order']['price'],99)
        self.step(START+300,b=book(START+300,size=queue))
        return self.l.get('s')

    def fill_buy(self):
        self.activate_buy(queue=10)
        self.step(START+1000,[trade('initial',START-100),trade('b',START+900,99,210)])
        return self.l.get('s')

    def finish_cycle(self):
        t=self.fill_buy();qty=t['remaining_qty']
        self.step(START+1100,[trade('b',START+900,99,210)])
        self.assertEqual(self.l.get('s')['order']['price'],100)
        self.step(START+1400,[trade('b',START+900,99,210)],book(START+1400,bid=99,ask=100,size=10))
        self.step(START+2000,[trade('b',START+900,99,210),trade('s',START+1900,100,qty+10,True)])
        return self.l.get('s')

    def test_touch_and_wrong_aggressor_never_fill(self):
        t=self.activate_buy()
        for i in range(1,4):
            self.step(START+1000*i,[trade('initial',START-100),trade(str(i),START+1000*i-20,99,10000,True)])
        self.assertEqual(self.l.get('s')['remaining_qty'],0)
        self.assertEqual(self.l.account('upbit')['cash_quote'],1000000)

    def test_queue_consumption_partial_buy_cancel_and_immediate_sell_phase(self):
        self.activate_buy(queue=100)
        self.step(START+1000,[trade('initial',START-100),trade('a',START+900,99,90)])
        self.assertEqual(self.l.get('s')['order']['queue_ahead'],10)
        self.assertEqual(self.l.get('s')['remaining_qty'],0)
        self.step(START+1500,[trade('a',START+900,99,90),trade('b',START+1400,99,20)])
        t=self.l.get('s')
        self.assertEqual(t['remaining_qty'],10)
        self.assertIsNone(t['order'])
        self.assertAlmostEqual(t['entry_cost'],990*1.0005)
        self.assertAlmostEqual(t['session_cash'],200000-t['entry_cost'])

    def test_cycle_restarts_and_return_accounts_for_all_fees(self):
        t=self.finish_cycle()
        self.assertEqual(t['cycles_completed'],1)
        self.assertEqual(t['remaining_qty'],0)
        self.assertEqual(t['status'],'OPEN')
        self.assertAlmostEqual(t['gross_pnl']-t['fees_paid'],t['realized_quote'])
        self.assertAlmostEqual(self.l.account('upbit')['cash_quote'],1000000+t['realized_quote'])
        self.step(START+2100,[trade('s',START+1900,100,1000,True)])
        self.assertEqual(self.l.get('s')['order']['side'],'BUY')
        self.assertEqual(self.l.get('s')['deadline_ms'],START+DEADLINE_MS)

    def test_submission_delay_excludes_previous_trades(self):
        self.step(START)
        self.step(START+200,[trade('early',START+100,99,99999)],book(START+200))
        self.assertIsNone(self.l.get('s')['order']['active_ms'])
        self.step(START+300,[trade('early',START+100,99,99999)],book(START+300))
        self.assertEqual(self.l.get('s')['remaining_qty'],0)

    def test_duplicate_tape_cannot_fill_twice(self):
        self.activate_buy(queue=100)
        rows=[trade('initial',START-100),trade('a',START+900,99,90)]
        self.step(START+1000,rows);self.step(START+1100,rows)
        self.assertEqual(self.l.get('s')['order']['queue_ahead'],10)
        self.assertEqual(self.l.get('s')['remaining_qty'],0)

    def test_nonmonotonic_unique_ids_use_time_not_numeric_order(self):
        self.activate_buy(queue=10)
        self.step(START+1000,[trade('initial',START-100),trade('999',START+900,99,5)])
        self.step(START+1200,[trade('999',START+900,99,5),trade('1',START+1100,99,10)])
        self.assertEqual(self.l.get('s')['remaining_qty'],5)

    def test_gap_and_restart_do_not_backfill_or_reset_deadline(self):
        self.activate_buy(queue=100)
        self.l.close();self.l=TickLedger(self.path,START+1000)
        self.step(START+2000,[trade('new',START+1900,99,10000)])
        self.assertEqual(self.l.get('s')['data_gaps'],1)
        self.assertEqual(self.l.get('s')['remaining_qty'],0)
        self.step(START+2200,[trade('new',START+1900,99,10000)],book(START+2200,size=150))
        self.assertEqual(self.l.get('s')['order']['queue_ahead'],150)
        self.assertEqual(self.l.get('s')['deadline_ms'],START+DEADLINE_MS)

    def test_exact_deadline_cancels_unfilled_buy_without_debit(self):
        self.activate_buy()
        self.step(START+DEADLINE_MS,[trade('initial',START-100),trade('late',START+DEADLINE_MS-1,99,99999)])
        t=self.l.get('s')
        self.assertEqual(t['status'],'SKIPPED')
        self.assertEqual(t['reason'],'LIMIT_UNFILLED_10M')
        self.assertIsNone(t['order'])
        self.assertEqual(self.l.account('upbit')['cash_quote'],1000000)

    def test_deadline_forces_market_residual_and_never_starts_next_cycle(self):
        t=self.fill_buy()
        self.l.advance('upbit',START+DEADLINE_MS)
        self.assertEqual(self.l.get('s')['status'],'EXIT_PENDING')
        self.assertTrue(self.l.exit('s',book(START+DEADLINE_MS+300,bid=95,ask=96),START+DEADLINE_MS+300))
        t=self.l.get('s')
        self.assertEqual(t['status'],'CLOSED')
        self.assertLess(t['forced_exit_pnl'],0)
        self.assertEqual(t['cycles_completed'],0)
        self.assertEqual(t['forced_exits'],1)
        self.assertAlmostEqual(t['gross_pnl']-t['fees_paid']-t['slippage_paid'],t['realized_quote'])
        self.assertFalse(self.step(START+DEADLINE_MS+400))

    def test_flat_end_closes_session_and_releases_reserved_cash(self):
        self.finish_cycle()
        self.l.advance('upbit',START+DEADLINE_MS)
        t=self.l.get('s')
        self.assertEqual(t['status'],'CLOSED')
        self.assertEqual(t['reason'],'SESSION_10M')
        self.assertEqual(t['session_cash'],0)

    def test_reserved_capital_prevents_double_allocation(self):
        self.finish_cycle()
        for i in range(4):self.assertTrue(self.l.offer(str(i),'upbit','KRW-'+str(i),START+3000,START+3000))
        self.assertFalse(self.l.offer('six','upbit','KRW-SIX',START+3000,START+3000))
        self.assertEqual(self.l.get('six')['reason'],'ALL_SLOTS_USED')
        self.assertAlmostEqual(sum(t['session_cash'] for t in self.l.active('upbit')),self.l.account('upbit')['cash_quote'])

    def test_marketable_limit_uses_taker_fee_and_respects_limit(self):
        self.step(START)
        self.step(START+300,b=book(START+300,bid=97,ask=98,size=100))
        t=self.l.get('s')
        self.assertEqual(t['remaining_qty'],10)
        self.assertEqual(t['taker_fills'],1)
        self.assertAlmostEqual(t['entry_cost'],980*1.0005)

    def test_partial_sell_keeps_residual_until_more_trade_volume(self):
        t=self.fill_buy();qty=t['remaining_qty']
        self.step(START+1100,[trade('b',START+900,99,210)])
        self.step(START+1400,[trade('b',START+900,99,210)],book(START+1400,bid=99,ask=100,size=10))
        self.step(START+2000,[trade('b',START+900,99,210),trade('s1',START+1900,100,30,True)])
        self.assertAlmostEqual(self.l.get('s')['remaining_qty'],qty-20)
        self.assertEqual(self.l.get('s')['order']['side'],'SELL')
        self.step(START+2400,[trade('s1',START+1900,100,30,True),trade('s2',START+2300,100,qty,True)])
        self.assertEqual(self.l.get('s')['cycles_completed'],1)

    def test_old_market_trade_keeps_original_exit_and_history(self):
        self.l.close();old=PaperLedger(self.path,START)
        old.offer('old','binance','ABCUSDT',START,START)
        old.fund('binance',1400,'test',START)
        old.offer('legacy','binance','ABCUSDT',START,START)
        old.enter('legacy',book(START+300,bid=100,ask=100.01,size=10000),START+300)
        original=old.get('legacy');old.close()
        self.l=TickLedger(self.path,START+300000)
        self.l.advance('binance',original['exit_due_ms'])
        self.assertEqual(self.l.get('legacy')['reason'],'HOLD_5M')
        self.assertTrue(self.l.exit('legacy',book(original['exit_due_ms']+300,bid=101,ask=101.01),original['exit_due_ms']+300))
        self.assertEqual(self.l.get('legacy')['status'],'CLOSED')
        self.assertEqual(self.l.started_ms,START)

    def test_failed_depth_leaves_pending_and_retries_without_fake_fill(self):
        self.fill_buy();self.l.advance('upbit',START+DEADLINE_MS)
        bad=book(START+DEADLINE_MS+300);bad['bids']=[]
        with self.assertRaises(ValueError):self.l.exit('s',bad,START+DEADLINE_MS+300)
        self.assertEqual(self.l.get('s')['status'],'EXIT_PENDING')
        self.assertGreater(self.l.get('s')['remaining_qty'],0)

    def test_deadline_step_does_not_use_slow_limit_tape(self):
        self.fill_buy()
        self.l.advance('upbit',START+DEADLINE_MS)
        service=TickPaperService.__new__(TickPaperService)
        service.ledger=self.l;service.clock=lambda:START+DEADLINE_MS+500
        service.log=Mock();service.last_mark={'upbit':START+DEADLINE_MS+500}
        market=Mock();market.book.return_value=book(START+DEADLINE_MS+500,bid=95,ask=96)
        service.step('upbit',market)
        market.tape.assert_not_called()
        self.assertEqual(self.l.get('s')['status'],'CLOSED')

    def test_two_cycles_compound_cash_but_return_uses_reserved_capital(self):
        self.finish_cycle()
        self.step(START+2100,[trade('s',START+1900,100,1000,True)])
        self.step(START+2400,[trade('s',START+1900,100,1000,True)],book(START+2400,size=10))
        self.step(START+3000,[trade('s',START+1900,100,1000,True),trade('b2',START+2900,99,210)])
        self.step(START+3100,[trade('b2',START+2900,99,210)])
        self.step(START+3400,[trade('b2',START+2900,99,210)],book(START+3400,bid=99,ask=100,size=10))
        self.step(START+4000,[trade('b2',START+2900,99,210),trade('s2',START+3900,100,210,True)])
        self.l.advance('upbit',START+DEADLINE_MS)
        t=self.l.get('s')
        self.assertEqual(t['cycles_completed'],2)
        self.assertAlmostEqual(t['realized_quote'],2*(200-200*99*.0005-200*100*.0005))
        self.assertAlmostEqual(return_pct(t),t['realized_quote']/200000*100)
        self.assertAlmostEqual(self.l.account('upbit')['cash_quote'],1000000+t['realized_quote'])
        self.assertIn('왕복 2회',recent(self.l,START+DEADLINE_MS))

    def test_unfilled_is_visible_but_not_counted_as_closed_or_win(self):
        self.activate_buy();self.l.advance('upbit',START+DEADLINE_MS)
        self.assertIn('매수 미체결 · 0.00%',recent(self.l,START+DEADLINE_MS))
        a=self.l.snapshot(START+DEADLINE_MS)['accounts'][0]
        self.assertEqual((a['closed'],a['wins'],a['skipped']),(0,0,1))
        self.assertEqual(a['cohorts'],{})

    def test_reports_separate_fees_gross_forced_and_legacy(self):
        self.fill_buy();self.l.advance('upbit',START+DEADLINE_MS)
        self.l.exit('s',book(START+DEADLINE_MS+300,bid=95,ask=96),START+DEADLINE_MS+300)
        snapshot=self.l.snapshot(START+DEADLINE_MS+300)
        c=snapshot['accounts'][0]['cohorts'][VERSION]
        self.assertAlmostEqual(c['gross_krw']-c['fees_krw']-c['slippage_krw'],c['closed_trade_pnl_krw'])
        body=summary(snapshot)
        for label in ['v3 1틱 반복','강제청산 순손익','0.4/0.8%']:self.assertIn(label,body)
        self.assertLess(len(body),3500)
        self.assertIn('첫 매수',history(self.l,START+DEADLINE_MS+300))

    def test_partial_market_exit_retries_only_new_depth(self):
        self.fill_buy();self.l.advance('upbit',START+DEADLINE_MS)
        b=book(START+DEADLINE_MS+300,bid=95,ask=96,size=10)
        self.assertTrue(self.l.exit('s',b,START+DEADLINE_MS+300))
        self.assertEqual(self.l.get('s')['remaining_qty'],170)
        b.update(requested_ms=START+DEADLINE_MS+480,received_ms=START+DEADLINE_MS+500)
        self.assertFalse(self.l.exit('s',b,START+DEADLINE_MS+500))
        self.assertTrue(self.l.exit('s',book(START+DEADLINE_MS+800,bid=94,ask=95),START+DEADLINE_MS+800))
        self.assertEqual(self.l.get('s')['status'],'CLOSED')

    def test_minimum_exit_failure_never_fabricates_liquidation(self):
        self.fill_buy();self.l.advance('upbit',START+DEADLINE_MS)
        with self.l.lock,self.l.db:
            t=self.l._trade('s');t['rules']['min_notional']=1e9;self.l._save(t)
        self.assertFalse(self.l.exit('s',book(START+DEADLINE_MS+300),START+DEADLINE_MS+300))
        self.assertEqual(self.l.get('s')['last_error'],'DUST_BELOW_MARKET_MINIMUM')
        self.assertEqual(self.l.get('s')['status'],'EXIT_PENDING')

    def test_new_session_cannot_use_legacy_market_entry_path(self):
        self.assertFalse(self.l.enter('s',book(START+300),START+300))
        self.assertEqual(self.l.get('s')['remaining_qty'],0)

    def test_decimal_partial_fills_do_not_create_one_step_dust(self):
        with self.l.lock,self.l.db:
            t=self.l._trade('s')
            self.l._fill_tick(t,'BUY',.3,30,START,'MAKER',{})
            self.l._fill_tick(t,'SELL',.1,10,START+1,'MAKER',{})
            self.l._fill_tick(t,'SELL',.2,20,START+2,'MAKER',{})
            self.assertEqual(t['remaining_qty'],0)
            self.assertEqual(t['cycles_completed'],1)


class AdapterTests(unittest.TestCase):
    def test_official_public_trade_shapes_and_aggressor_direction(self):
        fixtures=[('upbit',[dict(sequential_id=17899687911240000,timestamp=START,
                      trade_price=84.3,trade_volume=2463.26,ask_bid='BID')],True),
                  ('bithumb',[dict(sequential_id=17899687915900000,timestamp=START,
                      trade_price=84.08,trade_volume=5005.2383539,ask_bid='ASK')],False),
                  ('binance',[dict(a=123,T=START,p='100',q='2',m=True)],False),
                  ('kraken',{'result':{'ZETAUSD':[['.0621','594',START/1000,'s','l','',175718]],'last':'x'}},False)]
        for venue,payload,buyer in fixtures:
            m=TickMarket.__new__(TickMarket);m.venue=venue;m.market=Mock()
            m.market.get.return_value=payload
            with patch('magi2.fast_tick_service.now',return_value=START):data=m.tape('TEST')
            rows=validate_tape(data,START)
            self.assertEqual(rows[0]['buyer'],buyer)
            self.assertEqual(rows[0]['ts'],START)

    def test_exchange_metadata_cache_and_market_lot_fallback(self):
        m=TickMarket.__new__(TickMarket);m.venue='binance';m.market=Mock();m.rule_cache={}
        m.market.get.return_value={'symbols':[dict(status='TRADING',filters=[
            dict(filterType='PRICE_FILTER',tickSize='.01',minPrice='.01',maxPrice='1000000'),
            dict(filterType='LOT_SIZE',stepSize='.001',minQty='.001',maxQty='9000'),
            dict(filterType='MARKET_LOT_SIZE',stepSize='0',minQty='0',maxQty='100'),
            dict(filterType='NOTIONAL',minNotional='5')])]}
        r=m.rules('ABC');self.assertEqual(r['market_step'],'.001')
        self.assertEqual(m.rules('ABC'),r);m.market.get.assert_called_once()


if __name__=='__main__':unittest.main()
