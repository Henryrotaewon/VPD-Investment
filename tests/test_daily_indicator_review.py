from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from magi2.fast_wave_indicator import Candle, DAY_MS
from magi2.daily_indicator_review import review, composite_review, COMPONENTS
from magi2.indicator_paper import IndicatorLedger, IndicatorPaperService
from magi2.indicator_monitor import IndicatorMonitor
from magi2.fast_paper_report import menu
from test_indicator_paper import decision, book, RULES, T


def history(end, offset=0):
    return [Candle(end-(130-i)*DAY_MS+offset,110,90,100,1000) for i in range(130)]


class DailyReviewTests(unittest.TestCase):
    def test_day11_signal_day12_boundary_and_no_lookahead(self):
        end=int(datetime(2026,9,12,tzinfo=timezone.utc).timestamp()*1000)
        rows=history(end)
        original=review(rows,end,venue='upbit',symbol='TEST')
        future=Candle(end,999999,1,999999,1e20)
        self.assertEqual(review(rows+[future],end,venue='upbit',symbol='TEST'),original)
        self.assertEqual(original['signal_day_ms'],end-DAY_MS)
        self.assertEqual(original['signal_available_ms'],end)
        self.assertEqual(original['next_daily_open_ms'],end)
        self.assertIsNone(original['signal']);self.assertIsNone(original['composite'])
        self.assertFalse(original['entry_enabled'])
        before=review(rows,end-1,venue='upbit',symbol='TEST')
        self.assertEqual(before['signal_day_ms'],end-2*DAY_MS)

    def test_signed_differences_and_explicit_composite(self):
        changes={k:dict(velocity=0.,acceleration=0.) for k in COMPONENTS}
        # Negative Williams values increasing -95 -> -90 -> -80: +10 velocity, +5 acceleration.
        changes['williams']=dict(velocity=10.,acceleration=5.)
        result=composite_review(changes,{k:1. for k in COMPONENTS},{k:1. for k in COMPONENTS})
        self.assertEqual(result,dict(velocity=2.5,acceleration=1.25))
        with self.assertRaises(ValueError):composite_review(changes,{}, {})
        with self.assertRaises(ValueError):
            composite_review(changes,{k:1. for k in COMPONENTS},{k:0. for k in COMPONENTS})

    def test_flat_zero_macd_rsi_signal_and_daily_volume_acceleration(self):
        end=200*DAY_MS;rows=history(end)
        rows[-3:]=[replace(r,volume=v) for r,v in zip(rows[-3:],(1000,1100,1400))]
        result=review(rows,end,venue='upbit',symbol='T')
        self.assertEqual(result['changes']['volume'],dict(previous_velocity=100,velocity=300,acceleration=200))
        self.assertEqual(result['changes']['macd_hist']['acceleration'],0)
        self.assertIsNone(result['observations'][-1]['values']['rsi_signal9'])
        for method in ('SMA','EMA'):
            r=review(rows,end,venue='upbit',symbol='T',rsi_signal_method=method)
            self.assertEqual(r['observations'][-1]['values']['rsi_signal9'],50)

    def test_native_bithumb_boundary_and_missing_days(self):
        end=200*DAY_MS;offset=15*3600000;rows=history(end,offset)
        result=review(rows,end+offset,venue='bithumb',symbol='T')
        self.assertEqual(result['next_daily_open_ms'],end+offset)
        with self.assertRaises(ValueError):review(rows,end+offset,venue='upbit',symbol='T')
        with self.assertRaises(ValueError):review(rows[:50]+rows[51:],end+offset,venue='bithumb',symbol='T')


class ReviewHoldTests(unittest.TestCase):
    def test_hold_cancels_only_pending_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'paper.db';ledger=IndicatorLedger(path,T-180000)
            self.assertTrue(ledger.offer_decision(decision(),100,T,T,0))
            ident=ledger.active('upbit')[0]['id']
            self.assertTrue(ledger.enter_market(ident,book(T+500),RULES,T+500))
            self.assertTrue(ledger.offer_decision(decision(T+1000,symbol='PENDING'),100,T+1000,T+1000,0))
            self.assertEqual(ledger.suspend_for_daily_review(T+1100),1)
            self.assertEqual(ledger.get(ident)['status'],'OPEN')
            self.assertFalse(ledger.control()['enabled'])
            self.assertFalse(ledger.resume(T+1200))
            self.assertFalse(ledger.offer_decision(decision(T+2000),100,T+2000,T+2000,0))
            self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM paper_fills WHERE side='SELL'").fetchone()[0],0)
            ledger.close();ledger=IndicatorLedger(path,T+3000)
            self.assertTrue(ledger.policy_review_required);self.assertFalse(ledger.resume(T+4000))
            ledger.close()

    def test_production_service_and_start_button_cannot_resume_trial(self):
        from magi2 import server_runner as server
        with tempfile.TemporaryDirectory() as root:
            paper=IndicatorPaperService(root,lambda _:None,clock=lambda:T)
            self.assertFalse(paper.ledger.control()['enabled'])
            monitor=IndicatorMonitor(root,lambda _:None,paper)
            monitor.start();self.assertEqual(monitor.threads,[])
            self.assertIn('검토',menu(paper.ledger)[0])
            self.assertIn('아직 시작하지',monitor.summary())
            with patch.object(server,'FAST_PAPER',paper),patch.object(server,'may_execute',return_value=True), \
                 patch.object(server,'telegram') as send,patch.object(server,'CONFIRMATIONS') as confirm:
                server.handle_command('fast_start','7','7')
                self.assertIn('검토 중',send.call_args.args[0]);confirm.issue.assert_not_called()
            paper.ledger.close()


if __name__=='__main__':unittest.main()
