import copy
import json
from pathlib import Path
import tempfile
import unittest

from magi2.fast_models.bear_signals import evaluate, FIVE
from magi2.fast_models.derivatives import Derivatives
from tests.test_fast_models import q,fx,feed,START


def declining():
    end=START//FIVE*FIVE
    rows=[]
    for i in range(100):
        c=100-i*.05
        rows.append([end-(100-i)*FIVE,c+.05,c+.4,c-.4,c,10,1000])
    rows[-1][1:]=[95.1,95.3,94.1,94.3,20,2000]
    return rows,end+3000


class NativeSignalTests(unittest.TestCase):
    def test_breakdown_native_turnover_and_no_future_candles(self):
        rows,now=declining()
        regime=dict(state='DOWN',ts=now)
        result=evaluate(rows,now,regime)
        self.assertEqual(result['reason'],'QUALIFIED',result)
        self.assertIn('SHORT_BREAKDOWN',[s['model'] for s in result['signals']])
        self.assertEqual(result,evaluate(rows+[[now,1,2,.5,1,10000,10000]],now,regime))
        for signal in result['signals']:
            self.assertEqual(signal['target_distance'],2*signal['stop_distance'])

    def test_regime_gap_and_turnover_reject(self):
        rows,now=declining()
        self.assertEqual(evaluate(rows,now,dict(state='OTHER',ts=now))['reason'],'BTC_NOT_DOWN')
        gapped=rows[:50]+rows[51:]
        self.assertIn('GAP',evaluate(gapped,now,dict(state='DOWN',ts=now))['reason'])
        rows[-1][-1]=100
        self.assertFalse(evaluate(rows,now,dict(state='DOWN',ts=now))['signals'])

    def test_retest_requires_past_break_and_rejection_not_future_pivot(self):
        rows,now=declining()
        rows[-6][1:]=[95.5,95.7,94.5,94.7,20,2000]
        rows[-2][1:]=[95.3,95.5,95.0,95.2,10,1000]
        rows[-1][1:]=[95.4,95.7,94.5,94.7,20,2000]
        r=evaluate(rows,now,dict(state='DOWN',ts=now))
        self.assertIn('SHORT_RETEST',[s['model'] for s in r['signals']],r)


class ShortRiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Derivatives(Path(self.tmp.name)/'p.db',START)
        self.p.initialize_wallets(fx(),START)
        self.features=dict(stop_distance=.01,stop_price=101,target_distance=.02,max_hold_ms=7200000)
    def tearDown(self):self.p.db.close();self.tmp.cleanup()

    def test_new_short_shares_existing_wallet_and_causal_fill(self):
        a=q()
        self.p.reserve('new','SHORT_BREAKDOWN',None,a,10,fx(),START,dict(self.features))
        self.assertIn('new',self.p.s['pending'])
        self.p.enter_pending({a['id']:a},fx(),{a['id']:feed()},START+1,dict(state='DOWN',ts=START))
        self.assertFalse(self.p.s['positions'])
        a.update(ts=START+1000,exchange_ts=START+1000)
        self.p.enter_pending({a['id']:a},fx(),{a['id']:feed()},START+1000,dict(state='DOWN',ts=START))
        self.assertIn('new',self.p.s['positions'])
        self.assertEqual(self.p.s['initial'],3000000)

    def test_daily_loss_blocks_new_short_but_preserves_cash_and_existing_account(self):
        self.p.metrics.trade('loss',START,'SHORT',-61000,{})
        before=copy.deepcopy(self.p.s['wallets'])
        self.p.reserve('new','SHORT_RETEST',None,q(),10,fx(),START+1,dict(self.features))
        self.assertFalse(self.p.s['pending'])
        self.assertEqual(self.p.last_reserve_reason,'DAILY_LOSS_LIMIT')
        self.assertEqual(before,self.p.s['wallets'])

    def test_two_short_limit_and_no_positive_regime_fill(self):
        self.p.s['positions']={'a':dict(kind='SHORT',asset='ETH'),'b':dict(kind='SHORT_RETEST',asset='SOL')}
        self.p.reserve('new','SHORT_RETEST',None,q(),10,fx(),START,dict(self.features))
        self.assertEqual(self.p.last_reserve_reason,'SHORT_POSITION_LIMIT')
        self.p.s['positions']={}
        self.p.reserve('new','SHORT_RETEST',None,q(),10,fx(),START,dict(self.features))
        a=q(ts=START+1000)
        self.p.enter_pending({a['id']:a},fx(),{a['id']:feed()},START+1000,dict(state='OTHER',ts=START))
        self.assertFalse(self.p.s['positions'])
        self.assertFalse(self.p.s['pending'])


if __name__=='__main__':unittest.main()
