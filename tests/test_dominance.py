import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import requests
from magi1 import dominance as d
from magi2.market_context_view import render
from tests.test_market_context import NOW, bundle


def upbit_html():
    rows = [dict(code=key, tradePrice=value, prevClosingPrice=prior,
                 candleDateTime=datetime.fromtimestamp(NOW, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'))
            for key, value, prior in [('BTC', .63, .64), ('ETH', .12, .11),
                                      ('STABLE', .1, .1), ('ETC', .15, .15)]]
    node = {'queryKey': [['indicator', 'mdom', 'recents']], 'state': {'data': {'data': rows}}}
    # Actual Next.js transport, including a chunk boundary inside JSON.
    payload = '8:'+json.dumps(['$', {'queries': [node]}])+'\n'
    return ''.join('<script>self.__next_f.push('+json.dumps([1, chunk])+')</script>'
                   for chunk in (payload[:100], payload[100:]))


def global_inputs():
    return ({'updated_at': NOW, 'market_cap_percentage': {'btc': 60, 'eth': 12},
             'market_cap_change_percentage_24h_usd': 0},
            [dict(id=ident, market_cap_change_percentage_24h=change,
                  last_updated=datetime.fromtimestamp(NOW, timezone.utc).isoformat())
             for ident, change in [('bitcoin', 2), ('ethereum', -1)]])


class DominanceTests(unittest.TestCase):
    def test_upbit_public_json_sign_units_and_stable_separation(self):
        row = d.upbit_page(upbit_html(), NOW)
        self.assertEqual(row['assets']['BTC']['direction'], 'DOWN')
        self.assertAlmostEqual(row['assets']['BTC']['change_pp'], -1)
        self.assertAlmostEqual(row['assets']['STABLE']['share'], .1)
        with self.assertRaises(ValueError): d.upbit_page(upbit_html(), NOW+1801)
        with self.assertRaises(ValueError): d.upbit_page('<html>maintenance</html>', NOW)

    def test_global_24h_formula_and_timestamp_guard(self):
        data, coins = global_inputs()
        row = d.global_data(data, coins, NOW)
        self.assertAlmostEqual(row['assets']['BTC']['previous_share'], .6/1.02)
        self.assertEqual(row['assets']['BTC']['direction'], 'UP')
        self.assertEqual(row['assets']['ETH']['direction'], 'DOWN')
        self.assertEqual(row['comparison'], '24H_ESTIMATE')
        for value in (float('nan'), -100):
            bad = copy.deepcopy(coins); bad[0]['market_cap_change_percentage_24h'] = value
            with self.assertRaises(ValueError): d.global_data(data, bad, NOW)
        data['updated_at'] = NOW-901
        with self.assertRaisesRegex(ValueError, 'TIME_MISMATCH'): d.global_data(data, coins, NOW)

    def test_source_outage_does_not_hide_other_region_or_btc(self):
        data, coins = global_inputs()
        fetch = Mock(side_effect=[requests.Timeout(), {'data': data}, coins])
        rows = d.collect(fetch, NOW)
        self.assertEqual(rows['upbit']['status'], 'UNAVAILABLE')
        self.assertEqual(rows['global']['status'], 'OK')
        payload = bundle(); payload['dominance'] = rows
        text = render(payload, NOW)
        self.assertIn('국내 BTC: 오늘 상승', text)
        self.assertIn('BTC 도미넌스: 국내 자료 없음 · 해외(글로벌) 상승', text)

    def test_view_displays_directions_values_units_basis_and_rejects_bad_data(self):
        data, coins = global_inputs()
        p = bundle(); p['dominance'] = {'upbit': d.upbit_page(upbit_html(), NOW),
                                      'global': d.global_data(data, coins, NOW)}
        text = render(p, NOW)
        summary, details = text.split('📊 근거 데이터')
        self.assertIn('BTC 도미넌스: 국내 하락 · 해외(글로벌) 상승', summary)
        self.assertNotIn('메인·알트:', summary)
        self.assertIn('BTC 63.00% (-1.000%p)', details)
        self.assertIn('전일 종가 대비', details)
        self.assertIn('24시간 대비 환산 추정', details)
        self.assertLess(len(text), 4096)
        p['dominance']['upbit']['assets']['BTC']['change_pp'] = float('nan')
        self.assertIn('국내 자료 없음', render(p, NOW))


if __name__ == '__main__': unittest.main()
