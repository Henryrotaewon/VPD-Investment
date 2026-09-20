import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi2.fast_report import view, load_latest
from magi2.telegram_ui import parse_command
from magi2 import server_runner as server


class FastReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def save_run(self, trades, name='run-1', finished=2000):
        folder = self.root / name; folder.mkdir()
        manifest = {'run_id':name, 'finished_ms':finished, 'results':[{'venue':'upbit'}]}
        (folder/'result.json').write_text(json.dumps(manifest))
        report = {'schema':'fast-lab-report-v1', 'mode':'OFFLINE_REPLAY', 'policy_id':'policy-a',
                  'policy':{'fee_bps':5, 'slippage_bps':5, 'latency_ms':250, 'max_hold_ms':180000},
                  'trials':trades}
        (folder/'upbit-report.json').write_text(json.dumps(report))
        return folder

    def trade(self, ident='one', ret=100, cost=10000):
        return {'id':ident, 'mode':'OFFLINE_REPLAY', 'policy_id':'policy-a',
                'features':{'venue':'upbit','quote':'KRW','symbol':'KRW-TEST'},
                'entry':{'cost_quote':cost,'received_ts_ms':1000},
                'policy_exit':{'status':'COMPLETE','net_return_bps':ret,'received_ts_ms':1500,'reason':'TAKE_PROFIT'}}

    def test_missing_is_waiting_not_zero_profit(self):
        text, markup = view(self.root)
        self.assertIn('검증 결과 대기',text)
        self.assertNotIn('0원',text)
        self.assertTrue(markup['inline_keyboard'])

    def test_native_replay_producer_is_supported(self):
        from magi2.fast_lab.replay import Lab
        folder = self.save_run([])
        (folder/'upbit-report.json').write_text(json.dumps(Lab().finish()))
        text, _ = view(self.root)
        self.assertIn('완료 거래 표본 없음',text)
        self.assertNotIn('확인할 수 없습니다',text)

    def test_completed_only_cost_weighted_money_and_no_portfolio_claim(self):
        incomplete = self.trade('excluded'); incomplete['policy_exit']={'status':'EXIT_UNOBSERVABLE'}
        self.save_run([self.trade(), self.trade('two',-50,20000), incomplete])
        data=load_latest(self.root)
        self.assertEqual(len(data['valid']),2)
        self.assertEqual(sum(x['pnl'] for x in data['valid']),0)
        text,_=view(self.root)
        self.assertIn('평가 가능 2건 / 제외 1건',text)
        self.assertIn('1/2건 (50.0%)',text)
        self.assertIn('계좌 수익률·잔고·최대낙폭은 산출하지 않습니다',text)
        detail,_=view(self.root,True)
        self.assertIn('KRW-TEST',detail)
        self.assertIn('손익 -100원',detail)

    def test_invalid_latest_is_not_silently_replaced_with_old_success(self):
        self.save_run([self.trade()])
        folder=self.save_run([self.trade()],name='run-2',finished=3000)
        (folder/'upbit-report.json').write_text('{')
        text,_=view(self.root)
        self.assertIn('확인할 수 없습니다',text)
        self.assertNotIn('양수 거래',text)

    def test_duplicate_currency_and_nan_fail_closed(self):
        for kind in ('duplicate','currency','nan'):
            with self.subTest(kind=kind):
                folder=self.root/kind; folder.mkdir()
                t=self.trade()
                if kind=='currency':t['features']['quote']='USDT'
                if kind=='nan':t['policy_exit']['net_return_bps']=float('nan')
                report={'schema':'fast-lab-report-v1','mode':'OFFLINE_REPLAY','policy_id':'policy-a','policy':{},'trials':[t,t] if kind=='duplicate' else [t]}
                run=folder/'r';run.mkdir()
                (run/'result.json').write_text(json.dumps({'finished_ms':2000,'results':[{'venue':'upbit'}]}))
                (run/'upbit-report.json').write_text(json.dumps(report))
                self.assertIn('확인할 수 없습니다',view(folder)[0])

    def test_commands_callbacks_read_only(self):
        for name in ('/fast_report','FAST 모의결과','FAST 보고서'):
            self.assertEqual(parse_command(name),'fast_report')
        with patch.object(server,'telegram') as send, patch.object(server,'telegram_api'), \
             patch.object(server,'start_engine') as engine, patch.object(server,'ALLOWED_CHAT_ID','7'), \
             patch.dict('os.environ',{'FAST_PAPER_REPORT_DIR':str(self.root)}):
            server.handle_command('/fast_report','7','7')
            server.handle_callback({'id':'c','data':'nav:fast_orders','from':{'id':'7'},'message':{'chat':{'id':'7'}}})
            engine.assert_not_called()
            self.assertEqual(send.call_count,2)
            self.assertIn('검증 결과 대기',send.call_args.args[0])

if __name__=='__main__':unittest.main()
