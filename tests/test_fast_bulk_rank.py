import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from magi2.fast_bulk_rank import BulkRankMarket, FastBulkRankMonitor, gainers, BASIS
from magi2.fast_rank_paper import RankLedger, rank_day, INTERVAL

START=rank_day(1789970000000)+60000


class BulkTests(unittest.TestCase):
    def test_domestic_batches_no_per_symbol_history(self):
        for venue in ('upbit','bithumb'):
            market=BulkRankMarket(venue);market.symbols={f'KRW-X{i}':f'X{i}' for i in range(161)}
            def get(path,params):
                self.assertEqual(path,'/v1/ticker')
                return [dict(market=s,trade_price=110,opening_price=100) for s in params['markets'].split(',')]
            market.get=Mock(side_effect=get)
            with patch('magi2.fast_bulk_rank.time.sleep'):
                data,calls=market.day_tickers()
            self.assertEqual((len(data),calls,market.get.call_count),(161,3,3));market.http.close()
    def test_binance_day_batches_timezone_and_open_not_24h(self):
        market=BulkRankMarket('binance');market.symbols={f'X{i}USDT':f'X{i}' for i in range(201)}
        def get(path,params):
            self.assertEqual(path,'/api/v3/ticker/tradingDay');self.assertEqual(params['timeZone'],'0')
            symbols=json.loads(params['symbols']);self.assertLessEqual(len(symbols),100)
            return [dict(symbol=s,lastPrice='110',openPrice='100') for s in symbols]
        market.get=Mock(side_effect=get)
        with patch('magi2.fast_bulk_rank.time.sleep'):data,calls=market.day_tickers()
        self.assertEqual((len(data),calls),(201,3));market.http.close()
    def test_kraken_all_tickers_one_call_excludes_other_quote_markets(self):
        market=BulkRankMarket('kraken');market.symbols={'XUSD':'X','YUSD':'Y'}
        market.get=Mock(return_value=dict(result={'XUSD':dict(c=['110'],o='100'),
            'YUSD':dict(c=['210'],o='200'),'XEUR':dict(c=['500'],o='1')}))
        data,calls=market.day_tickers()
        self.assertEqual(calls,1);market.get.assert_called_once_with('/0/public/Ticker')
        self.assertEqual(data,{'XUSD':(110.,100.),'YUSD':(210.,200.)});market.http.close()
    def test_missing_duplicate_invalid_ticker_reject_whole_snapshot(self):
        market=BulkRankMarket('upbit');market.symbols={'X':'X','Y':'Y'}
        for rows in ([dict(market='X',trade_price=1,opening_price=1)],
                     [dict(market='X',trade_price=1,opening_price=1)]*2,
                     [dict(market='X',trade_price=float('nan'),opening_price=1)]):
            market.get=Mock(return_value=rows)
            with self.assertRaises(ValueError):market.day_tickers()
        market.http.close()
    def test_gainers_reference_is_open_and_ties_are_deterministic(self):
        symbols={str(i):str(i) for i in range(7)}
        rows=gainers({s:(100+int(s),100) for s in symbols},symbols)
        self.assertEqual([r['symbol'] for r in rows],['6','5','4','3','2'])
        self.assertEqual(rows[0]['basis'],BASIS);self.assertEqual(rows[0]['baseline_price'],100)
        self.assertNotIn('previous_close',rows[0])
        self.assertEqual(gainers({'B':(100,100),'A':(100,100)},{'B':'B','A':'A'})[0]['symbol'],'A')
    def test_zero_open_is_not_infinite_gainer(self):
        rows=gainers({'A':(100,0),'B':(101,100),'C':(0,0)},{'A':'A','B':'B','C':'C'})
        self.assertEqual([r['symbol'] for r in rows],['B'])
    def test_scan_preserves_rank_evidence_capital_and_existing_episode(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=RankLedger(Path(root)/'p.db',START)
            paper=Mock();paper.ledger=ledger;paper.wake={'upbit':Mock()}
            monitor=FastBulkRankMonitor(root,lambda _:None,paper,clock=lambda:START)
            market=Mock();market.symbols={'X':'X'};market.day_tickers.return_value=({'X':(110,100)},1)
            self.assertTrue(monitor.scan('upbit',market,START,0))
            t=ledger.active('upbit')[0]
            self.assertEqual(t['rank_basis'],BASIS);self.assertIsNone(t['previous_close'])
            self.assertEqual(t['baseline_price'],100);self.assertLessEqual(t['budget_quote'],200000)
            self.assertEqual(ledger.latest_rank('upbit')['basis'],BASIS)
            self.assertFalse(monitor.scan('upbit',market,START,0))
            self.assertEqual(len(ledger.active('upbit')),1)
            ledger.close()
    def test_slow_or_cross_day_sample_cannot_be_used(self):
        with tempfile.TemporaryDirectory() as root:
            market=Mock();market.symbols={'X':'X'};market.day_tickers.return_value=({'X':(110,100)},1)
            for clock in (Mock(side_effect=[START,START+15001]),
                          Mock(side_effect=[rank_day(START)-1,rank_day(START)+1])):
                monitor=FastBulkRankMonitor(root,lambda _:None,Mock(),clock=clock)
                with self.assertRaisesRegex(ValueError,'STALE_OR_CROSS_DAY'):monitor.sample(market)

if __name__=='__main__':unittest.main()
