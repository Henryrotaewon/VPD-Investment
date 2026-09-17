import asyncio
import gzip
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
from magi1.collectors.public_ws import Adapter,CollectorSupervisor,timestamp
from magi1.normalizer import trade,book
from magi1.features import FlowFeature,FeatureEngine
from magi1.formation import FormationEngine
from magi1.storage import Storage
from magi1.research import ResearchEngine
from magi1.evaluation import evaluate
from magi1.vpd_join import VPDPointInTimeJoin,snapshot
from magi1.universe import select,VENUES
from magi1.report import build_daily
from magi1.propagation import wilson

class Tests(unittest.TestCase):
    def test_subscriptions_arrays(self):
        for v in ('upbit','bithumb'):
            msgs=Adapter(v).subscriptions(['BTC'])
            self.assertEqual(len(msgs),1);self.assertIsInstance(msgs[0],list)
            self.assertEqual(msgs[0][1]['codes'],['KRW-BTC'])
        self.assertIn('ws-api.bithumb.com',Adapter('bithumb').url(['BTC']))

    def test_binance_partial_depth_missing_exchange_time(self):
        e=Adapter('binance').parse({'stream':'btcusdt@depth10@100ms','data':{'lastUpdateId':1,'bids':[['10','1']],'asks':[['11','1']]}},123)[0]
        self.assertEqual(e.mid,10.5);self.assertIsNone(e.exchange_ts_ms)

    def test_kraken_iso_timestamp(self):
        d={'channel':'trade','type':'update','data':[{'symbol':'BTC/USD','price':10,'qty':2,'timestamp':'2026-09-15T00:00:00.123456Z','side':'sell','trade_id':1}]}
        e=Adapter('kraken').parse(d,1)[0]
        self.assertEqual(e.exchange_ts_ms,1789430400123);self.assertEqual(e.side,'SELL')

    def test_coinone_ack_and_maker_direction(self):
        a=Adapter('coinone')
        self.assertEqual(a.parse({'response_type':'SUBSCRIBED','channel':'TRADE','data':{}},1),[])
        d={'response_type':'DATA','channel':'TRADE','data':{'quote_currency':'KRW','target_currency':'BTC','price':'1','qty':'2','timestamp':1,'is_seller_maker':True}}
        self.assertEqual(a.parse(d,2)[0].side,'BUY')
        d['data']['is_seller_maker']=False;self.assertEqual(a.parse(d,2)[0].side,'SELL')

    def test_bybit_delta_delete_and_reconnect_snapshot(self):
        a=Adapter('bybit')
        m={'topic':'orderbook.50.BTCUSDT','type':'snapshot','ts':1,'data':{'s':'BTCUSDT','u':2,'b':[['10','2'],['9','1']],'a':[['11','3']]}}
        a.parse(m,1)
        m.update(type='delta');m['data'].update(u=3,b=[['10','0']],a=[])
        self.assertEqual(a.parse(m,2)[0].best_bid,9)
        self.assertEqual(a.parse(m,3),[])
        with self.assertRaises(ValueError):Adapter('bybit').parse(m,4)

    def test_kraken_bad_checksum_rejected(self):
        a=Adapter('kraken')
        with self.assertRaises(ValueError):a.incremental('BTC/USD',[[10,1]],[[11,1]],True,depth=10,checksum=1)

    def test_sorted_book_and_zero_timestamp(self):
        b=book('coinone','BTC-KRW',[[9,1],[10,2]],[[12,1],[11,2]],None,received_ts_ms=0)
        self.assertEqual((b.best_bid,b.best_ask,b.received_ts_ms),(10,11,0))

    def test_universe_common_active_ranking(self):
        markets={v:{a:{'turnover':100-i,'pair':a,'quote':'USD'} for i,a in enumerate(['BTC','ETH','XRP','SOL','DOGE','ADA'])} for v in VENUES}
        markets['coinone'].pop('BTC')
        result=select(markets)
        self.assertEqual(len(result['assets']),5);self.assertNotIn('BTC',result['assets'])
        markets['coinone'].pop('ETH')
        with self.assertRaises(ValueError):select(markets)

    def feature(self,v,t,direction='BUY',horizon='MICRO'):
        s=1 if direction=='BUY' else -1
        return FlowFeature(v,'BTC',t,horizon,10*s,8*s,10,.7*s,1,3,10000,100)

    def test_ordered_states_stable_origin_and_timer(self):
        rows=[];e=FormationEngine(lambda k,r:rows.append((k,json.loads(json.dumps(r)))))
        for v,t in [('binance',1000),('upbit',1100),('bybit',1200),('bithumb',1300)]:e.ingest(self.feature(v,t))
        self.assertEqual([r['state'] for k,r in rows],['FORMATION','GLOBAL_CONSENSUS','KOREA_EARLY_RECEPTION','PROPAGATION'])
        e.ingest(self.feature('binance',2000));e.tick(31000);e.tick(301000)
        self.assertEqual(rows[-1][1]['state'],'NORMALIZATION')
        self.assertEqual(len({r['shock_id'] for k,r in rows}),1)
        self.assertEqual({r['origin_venue'] for k,r in rows},{'binance'})
        self.assertIsNone(rows[0][1]['origin_confidence'])

    def test_stale_consensus_not_accepted(self):
        rows=[];e=FormationEngine(lambda k,r:rows.append((k,r)))
        e.ingest(self.feature('binance',1000));e.ingest(self.feature('bybit',12000))
        self.assertEqual(len(rows),1)

    def test_sell_all_horizons(self):
        rows=[];e=FormationEngine(lambda k,r:rows.append(r))
        for h in ('MICRO','MESO','MACRO'):
            f=self.feature('binance',1000,'SELL',h)
            e.ingest(f)
        self.assertEqual(len(rows),3);self.assertTrue(all(r['direction']=='SELL' for r in rows))

    def test_features_warmup_bounded(self):
        e=FeatureEngine()
        self.assertEqual(e.ingest(trade('upbit','BTC-KRW',100,1,0,'BUY',received_ts_ms=0)),[])
        for sec in range(1,1300):e.ingest(trade('upbit','BTC-KRW',100+sec/1000,1,sec*1000,'BUY',received_ts_ms=sec*1000))
        self.assertLessEqual(len(e.buckets[('upbit','BTC')]),1201)

    def test_point_in_time_availability(self):
        j=VPDPointInTimeJoin([{'ts_ms':100,'available_ms':200,'assets':{'BTC':{'vpd':80}}}])
        self.assertIsNone(j.join(150,'BTC')['vpd']);self.assertEqual(j.join(200,'BTC')['vpd'],80)
        self.assertIsNone(j.join(200,'ETH')['vpd'])

    def test_mature_and_missing_evaluation(self):
        xs=[(i*1000,100+i/100) for i in range(11)]
        self.assertEqual(evaluate('x','FLOW_ONLY','BUY',100,xs,0,9000),[])
        rows=evaluate('x','FLOW_ONLY','BUY',100,xs,0,30000)
        self.assertEqual(rows[0]['status'],'COMPLETE');self.assertEqual(rows[1]['status'],'MISSING_DATA')
        self.assertIsNone(rows[1]['false_shock'])

    def test_research_end_to_end_restart_report(self):
        with tempfile.TemporaryDirectory() as d:
            s=Storage(d);e=ResearchEngine(s,['BTC'],0)
            e.add_vpd({'ts_ms':0,'available_ms':0,'event_ts_ms':0,'assets':{'BTC':{'vpd':85,'delta_vpd':10,'momentum':'↑'}}})
            for i in range(21):
                e.ingest(book('upbit','BTC-KRW',[[99,1]],[[101,1]],i*1000,received_ts_ms=i*1000))
                e.ingest(trade('upbit','BTC-KRW',100,1,i*1000,'BUY',received_ts_ms=i*1000))
                e.tick(i*1000)
            e.formation.ingest(self.feature('binance',20000))
            for i in range(21,3631):
                e.ingest(book('upbit','BTC-KRW',[[99,1]],[[101,1]],i*1000,received_ts_ms=i*1000))
                e.ingest(trade('upbit','BTC-KRW',100,1,i*1000,'BUY',received_ts_ms=i*1000));e.tick(i*1000)
            ev=s.query('evaluation',0,4000000)
            self.assertEqual({x['cohort'] for x in ev},{'VPD_ONLY','FLOW_ONLY','FLOW_VPD','FLOW_VPD_NON_REACTION'})
            self.assertEqual({x['horizon_sec'] for x in ev},{10,30,60,300})
            self.assertTrue(all(x['status']=='COMPLETE' for x in ev))
            e.checkpoint();count=len(ev);s.flush()
            r=ResearchEngine(s,['BTC'],4000000);r.tick(4000000)
            self.assertEqual(len(s.query('evaluation',0,5000000)),count)
            path=build_daily(s,datetime(1970,1,2,7,tzinfo=timezone(timedelta(hours=9))))
            self.assertIn('FLOW_VPD_NON_REACTION',path.read_text());s.close()
            self.assertTrue(list(Path(d).glob('raw/*.gz')))

    def test_wilson_interval(self):
        low,high=wilson(1,1);self.assertLess(low,.5);self.assertGreaterEqual(high,.99)

    def test_diagnostics_no_events_is_stale(self):
        async def sink(e):pass
        c=CollectorSupervisor(['BTC'],sink)
        self.assertTrue(all(x['stale'] for x in c.diagnostics().values()))

if __name__=='__main__':unittest.main()
