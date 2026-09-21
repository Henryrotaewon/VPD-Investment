import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from magi2.fast_paper import (PaperLedger, VENUES, KST, HOLD_MS, DEADLINE_MS,
                             checked_book, market_buy, market_sell, bounds)
from magi2.fast_paper_service import PaperService, PublicPaperMarket
from magi2.fast_paper_report import view, recent


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=KST).timestamp()*1000)


START = stamp('2026-09-21T10:00:00')


def book(ts, bid=100, ask=100.01, size=100000):
    return dict(requested_ms=ts, received_ms=ts+20,
                bids=[(bid, size)], asks=[(ask, size)])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'paper.sqlite3'
        self.ledger = PaperLedger(self.path, START)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def enter(self, ident='one', venue='upbit', ts=START, symbol=None):
        self.assertTrue(self.ledger.offer(ident, venue, symbol or 'KRW-'+ident, ts, ts))
        self.assertTrue(self.ledger.enter(ident, book(ts+250), ts+270))
        return self.ledger.get(ident)

    def close_trade(self, t, bid=102):
        due = t['exit_due_ms']
        self.ledger.advance(t['venue'], due)
        self.assertTrue(self.ledger.exit(t['id'], book(due+250, bid, bid+.01), due+270))
        return self.ledger.get(t['id'])

    def test_four_independent_seeds_fx_once_and_restart_preserves_cash(self):
        self.assertIsNone(self.ledger.account('binance')['cash_quote'])
        self.assertTrue(self.ledger.fund('binance', 1400, 'test', START))
        self.assertTrue(self.ledger.fund('kraken', 1420, 'test-usd', START))
        for a in self.ledger.snapshot(START)['accounts']:
            self.assertAlmostEqual(a['equity_krw'], 1_000_000)
        t = self.close_trade(self.enter())
        cash = self.ledger.account('upbit')['cash_quote']
        self.assertAlmostEqual(cash, 1_000_000+t['realized_quote'])
        self.ledger.close(); self.ledger = PaperLedger(self.path, START+86_400_000)
        self.assertEqual(self.ledger.started_ms, START)
        self.assertEqual(self.ledger.account('upbit')['cash_quote'], cash)
        self.assertEqual(self.ledger.account('bithumb')['cash_quote'], 1_000_000)
        self.assertFalse(self.ledger.fund('binance', 2000, 'replacement', START+1))
        self.assertEqual(self.ledger.account('binance')['fx_krw_per_quote'], 1400)

    def test_reservations_capacity_and_idempotence(self):
        for i in range(5):
            self.assertTrue(self.ledger.offer(str(i), 'upbit', 'KRW-'+str(i), START, START))
        self.assertFalse(self.ledger.offer('0', 'upbit', 'KRW-0', START, START))
        self.assertFalse(self.ledger.offer('six', 'upbit', 'KRW-6', START, START))
        self.assertEqual(self.ledger.get('six')['reason'], 'ALL_SLOTS_USED')
        for i in range(5):
            self.assertTrue(self.ledger.enter(str(i), book(START+250), START+270))
        self.assertAlmostEqual(self.ledger.account('upbit')['cash_quote'], 0, places=6)
        self.assertFalse(self.ledger.enter('0', book(START+300), START+320))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0], 5)
        self.assertEqual(self.ledger.account('bithumb')['cash_quote'], 1_000_000)

    def test_no_backfill_funding_gap_same_symbol_and_expired_entry(self):
        for ident, venue, signal, now, reason in [
            ('old','upbit',START-1,START,'STALE_SIGNAL'),
            ('late','upbit',START,START+10001,'STALE_SIGNAL'),
            ('future','upbit',START+1,START,'STALE_SIGNAL'),
            ('fx','binance',START,START,'FX_NOT_READY')]:
            self.assertFalse(self.ledger.offer(ident, venue, 'A', signal, now))
            self.assertEqual(self.ledger.get(ident)['reason'], reason)
        self.enter(symbol='KRW-A')
        self.assertFalse(self.ledger.offer('duplicate','upbit','KRW-A',START+1000,START+1000))
        self.assertEqual(self.ledger.get('duplicate')['reason'], 'SYMBOL_ALREADY_OPEN')
        self.assertTrue(self.ledger.offer('timeout','bithumb','KRW-A',START,START))
        self.ledger.advance('bithumb',START+10001)
        self.assertEqual(self.ledger.get('timeout')['reason'],'ENTRY_QUOTE_TIMEOUT')

    def test_depth_vwap_fees_and_slippage_conserve_money(self):
        asks = [(100, 100), (101, 1000)]
        qty, gross, fee, impact = market_buy(asks, 2000, .001, .0005)
        first = 10
        second = (2000/1.001-first*100*1.0005)/(101*1.0005)
        self.assertAlmostEqual(qty, first+second)
        self.assertAlmostEqual(gross+fee, 2000)
        self.assertAlmostEqual(impact, (first*100+second*101)*.0005)
        sold, proceeds, exit_fee, _ = market_sell([(99,5),(98,100)],qty,.001,.0005)
        self.assertAlmostEqual(sold,qty)
        self.assertAlmostEqual(proceeds,(5*99+(qty-5)*98)*.9995)
        self.assertAlmostEqual(exit_fee,proceeds*.001)

    def test_entry_needs_post_delay_fresh_finite_uncrossed_book(self):
        self.ledger.offer('one','upbit','KRW-A',START,START)
        for b, ts in [(book(START), START+270),
                      (book(START+250),START+4000),
                      (dict(book(START+250),received_ms=START+1800),START+1800),
                      (book(START+250,bid=102),START+270),
                      (book(START+250,ask=float('nan')),START+270),
                      (dict(book(START+250),bids=[]),START+270)]:
            with self.assertRaises(ValueError): self.ledger.enter('one',b,ts)
            self.assertEqual(self.ledger.account('upbit')['cash_quote'],1_000_000)
        self.assertTrue(self.ledger.enter('one',book(START+250),START+270))

    def test_wide_or_insufficient_depth_never_debits(self):
        for ident, b, reason in [('wide',book(START+250,ask=101),'ENTRY_SPREAD'),
                                ('thin',book(START+250,size=1),'INSUFFICIENT_ENTRY_DEPTH')]:
            self.ledger.offer(ident,'upbit','KRW-'+ident,START,START)
            self.assertFalse(self.ledger.enter(ident,b,START+270))
            self.assertEqual(self.ledger.get(ident)['reason'],reason)
        self.assertEqual(self.ledger.account('upbit')['cash_quote'],1_000_000)

    def test_five_minute_close_net_return_and_cooldown(self):
        t = self.enter()
        self.ledger.advance('upbit',t['exit_due_ms']-1)
        self.assertEqual(self.ledger.get('one')['status'],'OPEN')
        self.assertFalse(self.ledger.exit('one',book(t['exit_due_ms']),t['exit_due_ms']+20))
        done = self.close_trade(t)
        expected = t['entry_qty']*102*.9995*.9995-200000
        self.assertAlmostEqual(done['realized_quote'],expected)
        self.assertEqual(done['reason'],'HOLD_5M')
        self.assertEqual(done['deadline_delay_ms'],0)
        self.assertLess(done['close_ms']-START,DEADLINE_MS)
        self.assertFalse(self.ledger.offer('again','upbit',t['symbol'],done['close_ms'],done['close_ms']))
        self.assertEqual(self.ledger.get('again')['reason'],'COOLDOWN')

    def test_deadline_survives_restart_blocks_new_entry_and_records_late_recovery(self):
        t = self.enter()
        late = START+DEADLINE_MS+2000
        self.ledger.close(); self.ledger = PaperLedger(self.path,late)
        self.ledger.advance('upbit',late)
        self.assertEqual(self.ledger.get('one')['reason'],'DEADLINE_10M')
        self.assertFalse(self.ledger.offer('new','upbit','KRW-NEW',late,late))
        self.assertEqual(self.ledger.get('new')['reason'],'OVERDUE_EXIT')
        a = self.ledger.snapshot(late)['accounts'][0]
        self.assertEqual(a['overdue'],1); self.assertIsNone(a['equity_krw'])
        self.assertTrue(self.ledger.exit('one',book(late),late+20))
        done = self.ledger.get('one')
        self.assertEqual(done['deadline_delay_ms'],2020)
        self.assertEqual(self.ledger.snapshot(late+20)['accounts'][0]['late_closed'],1)

    def test_partial_exit_conserves_basis_and_does_not_reuse_same_book(self):
        t = self.enter(); due = t['exit_due_ms']
        self.ledger.advance('upbit',due)
        small = book(due+250,bid=102,ask=102.01,size=100)
        self.assertTrue(self.ledger.exit('one',small,due+270))
        partial = self.ledger.get('one')
        self.assertEqual(partial['status'],'EXIT_PENDING')
        self.assertAlmostEqual(partial['remaining_cost'],200000*(1-100/t['entry_qty']))
        cash = self.ledger.account('upbit')['cash_quote']
        self.assertFalse(self.ledger.exit('one',book(due+1000,bid=102,ask=102.01,size=100),due+1020))
        self.assertEqual(cash,self.ledger.account('upbit')['cash_quote'])
        self.assertTrue(self.ledger.exit('one',book(due+2000,bid=102,ask=102.01),due+2020))
        done = self.ledger.get('one')
        self.assertEqual(done['status'],'CLOSED')
        self.assertAlmostEqual(done['realized_quote'],t['entry_qty']*102*.9995*.9995-200000)
        self.assertAlmostEqual(self.ledger.account('upbit')['cash_quote'],1_000_000+done['realized_quote'])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM paper_fills WHERE side='SELL'").fetchone()[0],2)

    def test_reserved_entry_cannot_fill_after_another_position_passes_deadline(self):
        self.enter()
        ts=START+DEADLINE_MS-100
        self.assertTrue(self.ledger.offer('second','upbit','KRW-SECOND',ts,ts))
        self.assertFalse(self.ledger.enter('second',book(ts+250),ts+270))
        self.assertEqual(self.ledger.get('second')['reason'],'OVERDUE_EXIT')
        self.assertAlmostEqual(self.ledger.account('upbit')['cash_quote'],800000)

    def test_daily_fill_dates_cross_midnight_and_stale_marks(self):
        ts = stamp('2026-09-21T23:58:00')
        t = self.enter(ts=ts)
        self.assertIsNone(self.ledger.snapshot(ts+20000)['accounts'][0]['equity_krw'])
        self.ledger.mark('upbit',{t['symbol']:(101,101.01)},ts+20000)
        self.assertIsNotNone(self.ledger.snapshot(ts+20000)['accounts'][0]['equity_krw'])
        done = self.close_trade(t)
        before = self.ledger.snapshot(done['close_ms'],'2026-09-21')['accounts'][0]
        after = self.ledger.snapshot(done['close_ms'],'2026-09-22')['accounts'][0]
        self.assertEqual(before['bought'],1); self.assertEqual(before['closed'],0)
        self.assertEqual(before['pnl_krw'],0); self.assertGreater(before['fees_krw'],0)
        self.assertEqual(after['bought'],0); self.assertEqual(after['closed'],1)
        self.assertAlmostEqual(after['pnl_krw'],done['realized_quote'])
        self.assertEqual(bounds('2026-09-22')[0],stamp('2026-09-22T00:00:00'))

    def test_recent_button_latest_ten_includes_pending_without_fake_returns(self):
        for i in range(12):
            t = self.enter(str(i),ts=START+i*700000)
            self.close_trade(t)
        last = self.enter('latest',ts=START+12*700000)
        self.ledger.offer('skip','binance','NOT-INVESTED',last['entry_ms'],last['entry_ms'])
        text = recent(self.ledger,last['entry_ms'])
        lines = [x for x in text.splitlines() if ' | ' in x]
        self.assertEqual(len(lines),10)
        self.assertIn('KRW-latest',lines[0]); self.assertIn('보유 중',lines[0])
        self.assertNotIn('%',lines[0]); self.assertNotIn('NOT-INVESTED',text)
        self.assertIn('KRW-3',lines[-1]); self.assertIn('→',lines[1])
        self.assertEqual(view(self.ledger,last['entry_ms'])[0],text)
        self.ledger.advance('upbit',last['deadline_ms']+1)
        self.assertIn('청산 대기 · 10분 초과',recent(self.ledger,last['deadline_ms']+1))

    def test_daily_schedule_and_delivery_lease_persist(self):
        morning = stamp('2026-09-22T09:00:00')
        self.assertIsNone(self.ledger.due_day(START))
        self.assertIsNone(self.ledger.due_day(morning-1))
        self.assertEqual(self.ledger.due_day(morning),'2026-09-21')
        self.ledger.queue_daily('2026-09-21','frozen report',morning)
        self.assertEqual(self.ledger.claim_daily(morning),('2026-09-21','frozen report'))
        self.ledger.close(); self.ledger = PaperLedger(self.path,morning+1)
        self.assertIsNone(self.ledger.claim_daily(morning+1))
        self.ledger.queue_daily('2026-09-21','overwrite attempt',morning+1)
        self.assertEqual(self.ledger.claim_daily(morning+120000),('2026-09-21','frozen report'))
        self.ledger.daily_sent('2026-09-21')
        self.assertIsNone(self.ledger.claim_daily(morning+240000))
        self.assertIsNone(self.ledger.due_day(morning+240000))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.ts = START
        self.service = PaperService(self.tmp.name,lambda _:None,clock=lambda:self.ts)
        self.addCleanup(self.tmp.cleanup); self.addCleanup(self.service.ledger.close)
        self.market = Mock(spec=['fx','book','quotes'])
        self.market.fx.return_value=(1400,'test')
        def get_book(symbol):
            value=book(self.ts); self.ts+=20; return value
        self.market.book.side_effect=get_book
        self.market.quotes.side_effect=lambda symbols:({s:(100,100.01) for s in symbols},self.ts)

    def test_public_only_worker_closes_without_scanner_and_recovers_outage(self):
        self.assertTrue(self.service.offer('one','upbit','KRW-A',START))
        self.service.step('upbit',self.market); self.market.book.assert_not_called()
        self.ts+=250; self.service.step('upbit',self.market)
        self.assertEqual(self.service.ledger.get('one')['status'],'OPEN')
        self.ts=self.service.ledger.get('one')['exit_due_ms']
        self.service.step('upbit',self.market)
        self.ts+=250; saved=self.market.book.side_effect
        self.market.book.side_effect=TimeoutError()
        self.service.step('upbit',self.market)
        self.assertEqual(self.service.ledger.get('one')['status'],'EXIT_PENDING')
        self.assertEqual(self.service.ledger.get('one')['last_error'],'TimeoutError')
        self.ts=START+DEADLINE_MS+1; self.market.book.side_effect=saved
        self.service.step('upbit',self.market)
        self.assertEqual(self.service.ledger.get('one')['status'],'CLOSED')
        self.assertGreater(self.service.ledger.get('one')['deadline_delay_ms'],0)

    def test_daily_send_failure_retry_and_no_prestart_report(self):
        send=Mock(side_effect=TimeoutError())
        self.service.daily(send); send.assert_not_called()
        self.ts=stamp('2026-09-22T09:00:00')
        with self.assertRaises(TimeoutError): self.service.daily(send)
        self.assertLess(len(send.call_args.args[0]),3500)
        self.assertIn('2026-09-21',send.call_args.args[0])
        self.service.daily(send); self.assertEqual(send.call_count,1)
        self.ts+=120000; send.side_effect=None
        self.service.daily(send); self.assertEqual(send.call_count,2)
        self.ts+=120000; self.service.daily(send); self.assertEqual(send.call_count,2)

    def test_commands_and_buttons_only_read_ledger(self):
        from magi2 import server_runner as server
        from magi2.telegram_ui import parse_command, main_keyboard
        self.assertEqual(parse_command('📊 FAST 모의검증 결과'),'fast_report')
        self.assertIn('📊 FAST 모의검증 결과',str(main_keyboard()))
        with patch.object(server,'FAST_PAPER',self.service), patch.object(server,'telegram') as send, \
             patch.object(server,'telegram_api'), patch.object(server,'start_engine') as engine, \
             patch.object(server,'ALLOWED_CHAT_ID','7'):
            for command in ('fast_report','fast_balance','fast_orders','fast_daily'):
                server.handle_command('/'+command,'7','7')
                server.handle_callback({'id':'c','data':'nav:'+command,'from':{'id':'7'},'message':{'chat':{'id':'7'}}})
            engine.assert_not_called(); self.assertEqual(send.call_count,8)
            self.assertEqual(self.service.ledger.history(self.ts),[])

    def test_monitor_forwards_strong_signals_before_notification_gate(self):
        from magi2.fast_monitor import FastMonitor, Watch
        monitor=FastMonitor(self.tmp.name,lambda _:None,paper=Mock())
        self.addCleanup(monitor.audit.db.close)
        watch=Watch({'price':100,'returns_bps':100,'rank':1},START)
        watch.metrics={'ask':100.01}; watch.strength={'strong':False}
        monitor.record_signal('upbit','KRW-A',watch,START,20)
        monitor.paper.offer.assert_not_called()
        watch.strength={'strong':True}
        monitor.record_signal('upbit','KRW-A',watch,START+1,20)
        monitor.paper.offer.assert_called_once_with(watch.signal_id,'upbit','KRW-A',START+1)


class PublicAdapterTests(unittest.TestCase):
    def test_all_four_depth_formats_use_public_get(self):
        for venue in VENUES:
            with self.subTest(venue=venue), patch('magi2.fast_monitor.PublicMarket') as factory:
                market=factory.return_value
                if venue in ('upbit','bithumb'):
                    market.get.return_value=[{'market':'A','orderbook_units':[dict(bid_price=100,bid_size=1000,ask_price=100.01,ask_size=1000)]}]
                elif venue=='binance':
                    market.get.return_value={'bids':[['100','1000']],'asks':[['100.01','1000']]}
                else:
                    market.get.return_value={'result':{'A':{'bids':[['100','1000',123]],'asks':[['100.01','1000',123]]}}}
                with patch('magi2.fast_paper_service.now',side_effect=[START,START+20]):
                    result=PublicPaperMarket(venue).book('A')
                bids,asks=checked_book(result,START+20)
                self.assertEqual(bids,[(100,1000)]); self.assertEqual(asks,[(100.01,1000)])
                self.assertEqual(market.method_calls[0][0],'get')
                self.assertEqual(len(market.method_calls),1)

    def test_kraken_usd_is_not_assumed_equal_to_usdt(self):
        with patch('magi2.fast_monitor.PublicMarket') as factory:
            kraken=Mock(); upbit=Mock()
            factory.side_effect=[kraken,upbit]
            upbit.get.return_value=[{'trade_price':1400}]
            kraken.get.return_value={'result':{'USDTZUSD':{'c':['.98']}}}
            fx,source=PublicPaperMarket('kraken').fx()
            self.assertAlmostEqual(fx,1400/.98)
            self.assertIn('FIXED',source); upbit.http.close.assert_called_once()


if __name__=='__main__': unittest.main()
