import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from magi2.fast_volume import evaluate,parse_candles,preselect,VERSION,MINUTE
from magi2.fast_volume_monitor import FastVolumeMonitor,VolumeMarket
from magi2.fast_paper import PaperLedger
from magi2.fast_paper_report import recent,summary
from magi2.fast_comparison import aggregate
from magi2.fast_captures import captures


def candles():
    return [dict(start_ms=i*MINUTE,close=100+.04*i if i<=5 else 100.2+.4*(i-5),
                 turnover=100 if i<=5 else 300) for i in range(11)]


ASOF=11*MINUTE+3000


class VolumeTests(unittest.TestCase):
    def test_volume_and_acceleration_required_independently(self):
        rows=candles();r=evaluate(rows,ASOF)
        self.assertTrue(r['qualified']);self.assertEqual(r['turnover_ratio'],3)
        weak=copy.deepcopy(rows)
        for x in weak[6:]:x['turnover']=100
        self.assertEqual(evaluate(weak,ASOF)['reason'],'TURNOVER_BELOW_2X')
        slow=copy.deepcopy(rows);slow[0]['close']=90
        self.assertEqual(evaluate(slow,ASOF)['reason'],'PRICE_NOT_ACCELERATING')
        small=copy.deepcopy(rows);small[-1]['close']=100.5
        self.assertEqual(evaluate(small,ASOF)['reason'],'PRICE_RISE_BELOW_1PCT')

    def test_missing_zero_baseline_duplicate_and_nonfinite_rejected(self):
        rows=candles()
        self.assertEqual(evaluate(rows[:-1],ASOF)['reason'],'MISSING_CLOSED_MINUTES')
        self.assertEqual(evaluate(rows+[rows[-1]],ASOF)['reason'],'INVALID_OR_DUPLICATE_CANDLE')
        for x in rows[1:6]:x['turnover']=0
        self.assertEqual(evaluate(rows,ASOF)['reason'],'NO_TURNOVER_BASELINE')
        rows=candles();rows[-1]['turnover']=float('nan')
        self.assertFalse(evaluate(rows,ASOF)['qualified'])

    def test_forming_and_future_bars_cannot_change_signal(self):
        rows=candles();before=evaluate(rows,ASOF)
        rows+=[dict(start_ms=11*MINUTE,close=1e12,turnover=1e15)]
        self.assertEqual(before,evaluate(rows,ASOF))
        self.assertFalse(evaluate(candles(),ASOF+MINUTE)['qualified'])

    def test_all_exchange_turnover_parsers_use_quote_not_coin_units(self):
        kr={'result':{'PAIR':[[0,9,11,9,10,10,25,10]],'last':0}}
        bn=[[0,9,11,9,10,25,59999,250]]
        ko=[{'candle_date_time_utc':'1970-01-01T00:00:00','trade_price':10,'candle_acc_trade_price':250}]
        for venue,data in [('upbit',ko),('bithumb',ko),('binance',bn),('kraken',kr)]:
            self.assertEqual(parse_candles(venue,data),[{'start_ms':0,'close':10.,'turnover':250.}])

    def test_price_leader_not_displaced_by_rank_jump_and_budget_visible(self):
        rows=[{'symbol':str(i),'returns_bps':{'5':100+i},'rank_jump':1000-i} for i in range(30)]
        chosen,excluded=preselect(rows)
        self.assertEqual(chosen[0]['symbol'],'29');self.assertEqual(len(chosen),20)
        self.assertEqual(len(excluded),10)

    def test_public_adapter_uses_minute_candles(self):
        raw=[{'candle_date_time_utc':'1970-01-01T00:00:00','trade_price':10,'candle_acc_trade_price':250}]
        m=VolumeMarket('upbit')
        with patch.object(m,'get',return_value=raw) as get:
            self.assertEqual(m.candles('KRW-ZETA')[0]['turnover'],250)
            get.assert_called_once_with('/v1/candles/minutes/1',{'market':'KRW-ZETA','count':20})
        m.http.close()

    def test_wide_spread_enters_v2_and_costs_remain_real(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=PaperLedger(Path(root)/'paper.db',ASOF)
            self.assertTrue(ledger.offer('new','upbit','KRW-ZETA',ASOF,ASOF,strategy_version=VERSION))
            b={'requested_ms':ASOF+250,'received_ms':ASOF+270,'bids':[(56.5,100000)],'asks':[(56.6,100000)]}
            self.assertTrue(ledger.enter('new',b,ASOF+270))
            t=ledger.get('new');self.assertEqual(t['strategy_version'],VERSION)
            ledger.advance('upbit',t['exit_due_ms'])
            b.update(requested_ms=t['exit_due_ms']+250,received_ms=t['exit_due_ms']+270)
            self.assertTrue(ledger.exit('new',b,b['received_ms']))
            closed=ledger.get('new');self.assertLess(closed['realized_quote'],0)
            self.assertAlmostEqual(ledger.account('upbit')['cash_quote'],1000000+closed['realized_quote'])
            self.assertIn('v2',recent(ledger,b['received_ms']))
            self.assertIn('v2 거래량·가속',summary(ledger.snapshot(b['received_ms'])))
            ledger.close();ledger=PaperLedger(Path(root)/'paper.db',ASOF+1000000)
            self.assertIsNone(ledger.get('new')['entry_spread_limit_bps']);ledger.close()

    def test_legacy_pending_entry_keeps_original_spread_policy(self):
        with tempfile.TemporaryDirectory() as root:
            l=PaperLedger(Path(root)/'paper.db',ASOF);l.offer('old','upbit','KRW-ZETA',ASOF,ASOF)
            b={'requested_ms':ASOF+250,'received_ms':ASOF+270,'bids':[(56.5,100000)],'asks':[(56.6,100000)]}
            self.assertFalse(l.enter('old',b,ASOF+270));self.assertEqual(l.get('old')['reason'],'ENTRY_SPREAD');l.close()

    def test_emit_bypasses_old_filters_and_notification_gate_not_paper(self):
        with tempfile.TemporaryDirectory() as root:
            paper=Mock();m=FastVolumeMonitor(root,lambda _:None,paper)
            m.alert_gate.claim=Mock(return_value='GLOBAL_10_MINUTE_GAP')
            ev=evaluate(candles(),ASOF)
            self.assertTrue(m.emit('upbit','KRW-ZETA','ZETA',ev,56.5,56.6,ASOF,ASOF,1))
            paper.offer.assert_called_once()
            self.assertEqual(paper.offer.call_args.kwargs,{'strategy_version':VERSION})
            self.assertFalse(m.emit('upbit','KRW-ZETA','ZETA',ev,56.5,56.6,ASOF+1,ASOF,1))
            self.assertTrue(m.events.empty())
            text,_=captures(m.audit,ASOF)
            self.assertIn('v2 · 거래대금 3.00배',text)
            self.assertNotIn('강도 None',text)
            m.audit.db.close()

    def test_restart_signal_cooldown_is_durable(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('magi2.fast_volume_monitor.now',return_value=ASOF):
                m=FastVolumeMonitor(root,lambda _:None)
                m.emit('upbit','KRW-ZETA','ZETA',evaluate(candles(),ASOF),56.5,56.6,ASOF,ASOF,1)
                m.audit.db.close();m=FastVolumeMonitor(root,lambda _:None)
                self.assertFalse(m.emit('upbit','KRW-ZETA','ZETA',evaluate(candles(),ASOF),56.5,56.6,ASOF+1,ASOF,1))
                m.audit.db.close()

    def test_version_statistics_do_not_pool_different_rules(self):
        events=[dict(ts_ms=1,signal_id=v,event_type='SIGNAL_DETECTED',venue='upbit',rule_version=v) for v in ('fast-auto-v1',VERSION)]
        self.assertEqual(aggregate(events,0,2)[0]['signals'],1)
        self.assertEqual(aggregate(events,0,2,VERSION)[0]['signals'],1)


if __name__=='__main__':unittest.main()
