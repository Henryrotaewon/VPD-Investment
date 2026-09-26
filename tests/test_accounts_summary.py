import unittest
from magi3.accounts import render_accounts,holding_return


def position(asset,qty,value,cost=None,tags=None):
    return {'asset':asset,'qty':qty,'value_krw':value,'cost_krw':cost,'pick_basis':tags or ['UNATTRIBUTED']}

def venue(name,positions,status='OK',error=None):
    return {'venue':name,'positions':positions,'status':status,'error':error,
            'valued_total_krw':sum(p['value_krw'] for p in positions if p['value_krw'] is not None) if not error else None}

def report(venues):return {'venues':venues,'generated_ts_ms':1000}

class AccountSummaryTests(unittest.TestCase):
    def test_simple_layout_weighted_return_and_joint_strategy_once(self):
        ps=[position('KRW',1000,1000),position('BTC',1,120,100,['VPD']),position('ETH',2,90,100,['FAST','WAVE'])]
        r=report([venue('upbit',ps)])
        text=render_accounts(r,1000)
        self.assertIn('총 평가금액: 1,210원',text)
        self.assertIn('보유 수익률: +5.00%',text)
        self.assertIn('BTC 1개 · 120원 · +20.00% (VPD)',text)
        self.assertIn('VPD: 투자 100원 · +20.00%',text)
        self.assertIn('FAST+WAVE: 투자 100원 · -10.00%',text)
        self.assertNotIn('FAST: 투자',text)
        self.assertLess(text.index('업비트:'),text.index('픽 전략별'))
        for word in ('조회 전용','PARTIAL_VALUATION','관측','갱신','UNATTRIBUTED'):self.assertNotIn(word,text)
    def test_partial_and_unknown_basis_not_fabricated(self):
        ps=[position('BTC',1,100,80),position('GIFT',20,None)]
        r=report([venue('upbit',ps,'PARTIAL_VALUATION'),venue('binance',[],error='CREDENTIALS_NOT_READY')])
        text=render_accounts(r,1000)
        self.assertIn('총 평가금액: 100원 (확인분)',text)
        self.assertIn('보유 수익률: 미확인',text)
        self.assertIn('미분류: 투자 미확인 · 미확인',text)
        self.assertIn('바이낸스: 미연결',text)
        self.assertNotIn('GIFT 20개 · 0원',text)
    def test_missing_account_and_cash_only(self):
        r=report([venue('upbit',[],error='ACCOUNT_QUERY_FAILED')])
        self.assertIn('총 평가금액: 미확인',render_accounts(r,1000))
        self.assertIsNone(holding_return([position('KRW',100,100)]))
    def test_known_roi_not_lost_only_because_other_venue_unconfigured(self):
        r=report([venue('upbit',[position('BTC',1,90,100)]),venue('bithumb',[],error='CREDENTIALS_NOT_READY')])
        text=render_accounts(r,1000)
        self.assertIn('보유 수익률: -10.00%',text)
        self.assertIn('미분류: 투자 100원 · -10.00%',text)
