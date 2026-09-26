import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from magi1 import market_context as m
from magi1.intelligence_http import make_app
from magi2 import market_context_view as v

NOW = 1790420000


def quotes(alt_return=.02, btc_return=.01):
    rows = {str(i):dict(price=102,high=103,ts_ms=NOW*1000,turnover=100,**{'return':alt_return}) for i in range(30)}
    for a in ('BTC','ETH'):
        rows[a] = dict(price=101,high=102,ts_ms=NOW*1000,turnover=100,**{'return':btc_return})
    return rows


def metrics(day=.01):
    return dict(day_return=day, return_1h=.004,return_4h=.01,return_24h=.02,
                return_7d=.03,return_30d=.05,realized_vol_24h=.02,day_drawdown=-.01,
                hourly_close_ts_ms=int(NOW//3600)*3600000,daily_close_ts_ms=int(NOW//86400)*86400000,
                quote_ts_ms=NOW*1000)


def bundle(now=NOW):
    row=m.classify_venue(quotes(),32,metrics(),metrics())
    p=dict(schema_version=m.SCHEMA,policy_version=m.POLICY,producer='MAGI1',
           mode='OBSERVATION_ONLY',execution_eligible=False,observed_ts_ms=now*1000,
           generated_ts_ms=now*1000,expires_ts_ms=(now+m.TTL)*1000,
           day_start_ts_ms=int(now//86400)*86400000,venues={'upbit':row,'binance':copy.deepcopy(row)},
           macro={'DGS10':dict(label='미국10년금리',status='UNAVAILABLE')})
    return m.confirm(p,None,now)


class ContextTests(unittest.TestCase):
    def test_partial_forming_candles_cannot_change_returns(self):
        end=int(NOW//3600)*3600
        rows=[(end-i*3600,100-i) for i in range(1,26)]
        series,_=m.series_metrics(rows+[(end,'nan')],3600,NOW,25)
        self.assertEqual(series[-1],99)
        for bad in (rows[:-1],rows+[rows[0]],rows+[(end+7200,100)]):
            with self.assertRaises(ValueError): m.series_metrics(bad,3600,NOW,25)

    def test_direction_and_participation_are_independent(self):
        b=metrics(-.02);b.update(return_1h=-.01,return_4h=-.02)
        r=m.classify_venue(quotes(.01,-.02),32,b,metrics())
        self.assertEqual(r['direction'],'DOWN')
        self.assertEqual(r['participation'],'ALT_EXPANSION')
        r=m.classify_venue(quotes(-.02),32,metrics(),metrics())
        self.assertEqual(r['direction'],'UP')
        self.assertEqual(r['participation'],'MAJOR_LED')

    def test_low_coverage_and_small_universe_withhold(self):
        for q,n in [(quotes(),50),(dict(list(quotes().items())[:10]),32)]:
            with self.assertRaisesRegex(ValueError,'COVERAGE'):m.classify_venue(q,n,metrics(),metrics())

    def test_two_spaced_samples_confirm_not_duplicate_or_gap(self):
        old=bundle()
        for delta,expected in ((0,'PENDING'),(10,'PENDING'),(300,'CONFIRMED'),(601,'PENDING')):
            p=m.confirm(bundle(NOW+delta),old,NOW+delta)
            self.assertEqual(p['venues']['upbit']['confirmation'],expected)
        old['venues']['upbit']['observed_state']='DOWN/MIXED'
        self.assertEqual(m.confirm(bundle(NOW+300),old,NOW+300)['venues']['upbit']['confirmation'],'PENDING')

    def test_stale_future_nonfinite_invalid_schema_rejected(self):
        v.validate(bundle(),NOW)
        for mutate in (lambda p:p.update(expires_ts_ms=NOW*1000),
                       lambda p:p.update(observed_ts_ms=(NOW+100)*1000),
                       lambda p:p.update(execution_eligible=True),
                       lambda p:p.update(schema_version='bad'),
                       lambda p:p['venues']['upbit']['btc'].update(return_1h=float('nan'))):
            p=bundle();mutate(p)
            with self.assertRaises(ValueError):v.validate(p,NOW)

    def test_one_venue_failure_is_visible_without_relabeling_as_global(self):
        p=bundle();p['venues']['binance']={'status':'UNAVAILABLE'}
        text=v.render(p,NOW)
        self.assertIn('알트 확산',text);self.assertIn('해외 · Binance',text)
        self.assertIn('판단 보류',text);self.assertIn('실시간 아님',text)
        self.assertLess(len(text),4096)

    def test_failed_refresh_cannot_republish_old_classification(self):
        c=m.Collector()
        with patch.object(c,'upbit',side_effect=ValueError('MISSING_COMPLETED_CANDLES')),patch.object(c,'binance',return_value=bundle()['venues']['binance']),patch.object(c,'macro',return_value={}):
            p=c.collect(NOW,previous=bundle(NOW-300))
        self.assertEqual(p['venues']['upbit']['status'],'UNAVAILABLE')
        self.assertNotIn('direction',p['venues']['upbit'])

    def test_storage_is_bounded_and_latest_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=int(NOW//3600)*3600
            for age,offset in ((91*86400,0),(8*86400,0),(8*86400,300),(0,0)):
                m.persist(tmp,bundle(now-age+offset))
            with sqlite3.connect(Path(tmp)/'market_context.sqlite3') as db:
                rows=db.execute('SELECT bucket FROM snapshots').fetchall()
            self.assertEqual(sorted(t[0] for t in rows),[now-8*86400,now])
            self.assertEqual(json.loads((Path(tmp)/'exports/market_context_latest.json').read_text())['observed_ts_ms'],now*1000)

    def test_daily_macro_keeps_observation_dates_and_rate_units(self):
        class Response:
            text='observation_date,DGS10\n2026-09-24,4.00\n2026-09-25,4.10\n2026-09-26,99\n'
            def raise_for_status(self):pass
        c=m.Collector(get=lambda *a,**kw:Response())
        with patch.dict(m.FRED,{'DGS10':'미국10년금리'},clear=True):
            r=c.macro(NOW)['DGS10']
        self.assertEqual(r['observation_date'],'2026-09-25')
        self.assertAlmostEqual(r['change'],.1)
        self.assertEqual(r['change_unit'],'PERCENTAGE_POINTS')


class AdapterTests(unittest.TestCase):
    def test_upbit_day_open_not_previous_close_and_stables_excluded(self):
        assets=['BTC','ETH']+['A'+str(i) for i in range(30)]
        def fetch(url,params=None,*args,**kwargs):
            if url.endswith('/market/all'):
                return [{'market':'KRW-'+a} for a in assets+['USDE','XAUT']]
            if url.endswith('/ticker'):
                return [dict(market='KRW-'+a,trade_timestamp=NOW*1000,trade_price=102,
                             opening_price=100,prev_closing_price=80,high_price=104,acc_trade_price=100)
                        for a in assets]
            interval=3600 if '/minutes/' in url else 86400
            end=int(NOW//interval)*interval
            return [dict(candle_date_time_utc=m.datetime.fromtimestamp(end-i*interval,m.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),trade_price=100-i*.1)
                    for i in range(40 if interval==86400 else 30)]
        c=m.Collector()
        with patch.object(c,'fetch',side_effect=fetch):row=c.upbit(NOW)
        self.assertAlmostEqual(row['btc']['day_return'],.02)
        self.assertEqual(row['breadth']['eligible'],32)
        self.assertEqual(row['breadth']['alt_count'],30)

    def test_binance_requires_symbols_and_day_end_is_not_a_trade_timestamp(self):
        assets=['BTC','ETH','JUP','币安人生']+['A'+str(i) for i in range(130)]
        def fetch(url,params=None,*args,**kwargs):
            if url.endswith('/time'):return {'serverTime':NOW*1000}
            if url.endswith('/exchangeInfo'):
                return {'symbols':[dict(symbol=a+'USDT',baseAsset=a,quoteAsset='USDT',status='TRADING',isSpotTradingAllowed=True) for a in assets+['USDE','BTCUP']]}
            if url.endswith('/tradingDay'):
                requested=json.loads(params['symbols'])
                self.assertLessEqual(len(requested),100)
                self.assertTrue(set(requested).issubset({a+'USDT' for a in assets}))
                self.assertNotIn('\\u',params['symbols'])
                return [dict(symbol=a+'USDT',closeTime=(int(NOW//86400)+1)*86400000-1,openTime=int(NOW//86400)*86400000,
                             lastPrice=102,openPrice=100,highPrice=104,quoteVolume=100) for a in assets if a+'USDT' in requested]
            interval=3600 if params['interval']=='1h' else 86400
            end=int(NOW//interval)*interval
            return [[(end-i*interval)*1000,0,0,0,100-i*.1] for i in range(40 if interval==86400 else 30)]
        c=m.Collector()
        with patch.object(c,'fetch',side_effect=fetch): row=c.binance(NOW)
        self.assertEqual(row['breadth']['eligible'],134)  # JUP is not a leveraged token.
        def future(url,*a,**kw):
            data=fetch(url,*a,**kw)
            if url.endswith('/tradingDay'):
                for x in data:x['openTime']-=86400000
            return data
        with patch.object(c,'fetch',side_effect=future),self.assertRaises((KeyError,ValueError)):
            c.binance(NOW)
        def skew(url,*a,**kw):
            return {'serverTime':(NOW+60)*1000} if url.endswith('/time') else fetch(url,*a,**kw)
        with patch.object(c,'fetch',side_effect=skew),self.assertRaisesRegex(ValueError,'CLOCK_SKEW'):
            c.binance(NOW)

    def test_day_rollover_invalidates_old_context_even_within_ttl(self):
        midnight=int(NOW//86400)*86400
        p=bundle(midnight-60)
        with self.assertRaisesRegex(ValueError,'DAY'):v.validate(p,midnight+1)

    def test_view_fetch_uses_private_auth_no_redirects_and_no_exchange_fallback(self):
        import os
        from unittest.mock import Mock
        response=Mock();response.json.return_value=bundle()
        session=Mock();session.get.return_value=response
        with patch.dict(os.environ,{'MAGI1_MARKET_CONTEXT_PATH':'','MAGI1_INTELLIGENCE_URL':'http://magi1-flow.railway.internal:8081/intelligence','MAGI_INTELLIGENCE_TOKEN':'x'*32}),patch.object(v.requests,'Session') as factory:
            factory.return_value.__enter__.return_value=session
            self.assertEqual(v.fetch(NOW)['schema_version'],m.SCHEMA)
        self.assertEqual(session.get.call_args.args[0],'http://magi1-flow.railway.internal:8081/market-context')
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertFalse(session.trust_env)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_endpoint_requires_same_token_and_absence_is_503(self):
        from aiohttp.test_utils import TestClient,TestServer
        with tempfile.TemporaryDirectory() as root:
            async with TestClient(TestServer(make_app(root,'x'*32))) as client:
                r=await client.get('/market-context');self.assertEqual(r.status,401)
                headers={'Authorization':'Bearer '+'x'*32}
                r=await client.get('/market-context',headers=headers);self.assertEqual(r.status,503)
                m.persist(root,bundle())
                r=await client.get('/market-context',headers=headers);self.assertEqual(r.status,200)
                self.assertEqual((await r.json())['schema_version'],m.SCHEMA)


if __name__=='__main__': unittest.main()
