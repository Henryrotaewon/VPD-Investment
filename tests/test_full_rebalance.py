import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from magi2 import full_rebalance as rebuild
from magi2 import paper_engine as engine
from magi2 import server_runner as server
from magi2.telegram_ui import Confirmations, parse_command, vpd_keyboard

NOW=datetime.fromisoformat('2026-09-19T15:02:00+09:00')
ASOF=NOW-timedelta(minutes=2)


def fixture():
    cfg=copy.deepcopy(engine.CFG);cfg['session']['top_n']=2
    positions={c:dict(market='KRW-'+c,status='OPEN',qty=q,cost_krw=cost,entry_price=cost/q,
        entry_at='2026-09-18T07:30:00+09:00',hold_state='TREND',weakening_count=8)
        for c,q,cost in [('A',1,100),('B',2,200)]}
    positions['C']=dict(status='CLOSED',market='KRW-C',exit_at=NOW.isoformat(),exit_reason='HARD_STOP')
    state=dict(positions=positions,cash_krw=100,initial_cash_krw=1000,realized_pnl_krw=10,
               lifetime_realized_pnl_krw=50,last_rebalance_date=NOW.date().isoformat(),
               last_rebalance_vpd_asof=ASOF.isoformat(),closed_positions=[{'coin':'OLD','status':'CLOSED'}])
    rows=[dict(coin=c,market='KRW-'+c,Rank=i,VPD=90-i,price=100) for i,c in enumerate(('B','C'),1)]
    snap=dict(schema=engine.SCHEMA,basis='POINT_IN_TIME',asof=ASOF.isoformat(),
              generated_at=NOW.isoformat(),top10=rows,all_rows={r['coin']:r for r in rows})
    return cfg,state,snap,{'KRW-A':110,'KRW-B':90,'KRW-C':20}


class PlanningTests(unittest.TestCase):
    def test_sells_every_position_then_rebuilds_exact_latest_top_with_costs(self):
        cfg,state,snap,prices=fixture();before=copy.deepcopy(state)
        after,events,result=rebuild.plan_rebuild(state,snap,prices,cfg,NOW)
        self.assertEqual(state,before)
        self.assertEqual(result['sold'],['A','B'])
        self.assertEqual(result['bought'],['B','C'])
        self.assertEqual(result['repurchased'],['B'])
        self.assertEqual([e['type'] for e in events],['SELL','SELL','BUY','BUY','REBALANCE_FINISHED'])
        self.assertEqual({c for c,p in after['positions'].items() if p['status']=='OPEN'},{'B','C'})
        capital=100+(110+180)*(1-cfg['fee_rate'])
        self.assertAlmostEqual(result['allocated_krw'],capital)
        self.assertAlmostEqual(after['positions']['B']['cost_krw'],capital/2)
        self.assertAlmostEqual(after['positions']['C']['cost_krw'],capital/2)
        self.assertAlmostEqual(after['cash_krw'],0)
        self.assertAlmostEqual(after['positions']['B']['qty'],capital/2/(1+cfg['fee_rate'])/(90*(1+cfg['slippage_rate'])))
        self.assertEqual(after['positions']['A']['exit_reason'],'VPD_FULL_REBUILD')

    def test_reentry_is_explicit_and_history_principal_and_realized_pnl_survive(self):
        cfg,state,snap,prices=fixture()
        after,events,result=rebuild.plan_rebuild(state,snap,prices,cfg,NOW)
        self.assertEqual(after['initial_cash_krw'],1000)
        self.assertAlmostEqual(after['realized_pnl_krw'],10+result['sell_pnl_krw'])
        self.assertAlmostEqual(after['lifetime_realized_pnl_krw'],50+result['sell_pnl_krw'])
        self.assertEqual(after['closed_positions'][0],state['closed_positions'][0])
        archived={p['coin']:p for p in after['closed_positions']}
        self.assertEqual(archived['B']['exit_reason'],'VPD_FULL_REBUILD')
        self.assertEqual(archived['C']['exit_reason'],'HARD_STOP')
        self.assertEqual(after['positions']['C']['status'],'OPEN')
        self.assertNotIn('weakening_count',after['positions']['B'])
        self.assertTrue(all(e['paper_only'] for e in events))
        self.assertEqual(len({e['event_id'] for e in events}),len(events))

    def test_any_missing_invalid_price_or_incomplete_candidates_prevents_entire_plan(self):
        cfg,state,snap,prices=fixture();before=copy.deepcopy(state)
        for market in prices:
            for price in (None,0,-1,float('nan'),float('inf')):
                with self.assertRaises(ValueError):rebuild.plan_rebuild(state,snap,dict(prices,**{market:price}),cfg,NOW)
        for rows in (snap['top10'][:1],[snap['top10'][0]]*2):
            with self.assertRaises(ValueError):rebuild.plan_rebuild(state,dict(snap,top10=rows),prices,cfg,NOW)
        self.assertEqual(state,before)

    def test_live_mode_and_invalid_financial_inputs_are_rejected(self):
        cfg,state,snap,prices=fixture()
        with self.assertRaises(ValueError):rebuild.plan_rebuild(state,snap,prices,dict(cfg,mode='LIVE'),NOW)
        for cash in (-1,float('nan')):
            with self.assertRaises(ValueError):rebuild.plan_rebuild(dict(state,cash_krw=cash),snap,prices,cfg,NOW)


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state_path=Path(self.tmp.name)/'paper_state.json'
        self.events_path=Path(self.tmp.name)/'paper_events.jsonl'
        self.cfg,self.before,self.snap,self.prices=fixture()
        self.after,self.events,_=rebuild.plan_rebuild(self.before,self.snap,self.prices,self.cfg,NOW)
        self.state_path.write_text(json.dumps(self.before))
        self.old='{"type":"OLD","event_id":"legacy"}\n'
        self.events_path.write_text(self.old)

    def test_commit_keeps_prior_ledger_and_symlink_and_recovery_is_idempotent(self):
        link=self.state_path.with_name('linked.json');link.symlink_to(self.state_path)
        rebuild.commit_rebuild(link,self.events_path,self.before,self.after,self.events)
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(link.read_text()),self.after)
        lines=[json.loads(s) for s in self.events_path.read_text().splitlines()]
        self.assertEqual(lines[0]['type'],'OLD')
        self.assertEqual(len(lines),1+len(self.events))
        self.assertFalse(rebuild.recover_rebuild(link,self.events_path))

    def test_crash_between_state_and_ledger_recovers_without_duplicate_trades(self):
        atomic=rebuild.atomic_text
        def fail(path,text):
            if Path(path)==self.events_path:raise OSError('disk temporarily unavailable')
            return atomic(path,text)
        with patch.object(rebuild,'atomic_text',side_effect=fail):
            with self.assertRaises(OSError):rebuild.commit_rebuild(self.state_path,self.events_path,self.before,self.after,self.events)
        self.assertEqual(json.loads(self.state_path.read_text()),self.after)
        self.assertEqual(self.events_path.read_text(),self.old)
        with patch.object(engine,'CFG',self.cfg),patch.object(engine,'STATE_PATH',self.state_path),patch.object(engine,'EVENTS_PATH',self.events_path):
            recovered=engine.load_state()
            self.assertEqual(recovered,self.after)
            self.assertEqual(engine.load_state(),self.after)
        ids=[json.loads(s)['event_id'] for s in self.events_path.read_text().splitlines()]
        self.assertEqual(len(ids),len(set(ids)))

    def test_crash_before_state_write_and_after_ledger_write_both_recover_once(self):
        atomic=rebuild.atomic_text
        def fail(path,text):
            if Path(path)==self.state_path:raise OSError('interrupted')
            return atomic(path,text)
        with patch.object(rebuild,'atomic_text',side_effect=fail):
            with self.assertRaises(OSError):rebuild.commit_rebuild(self.state_path,self.events_path,self.before,self.after,self.events)
        self.assertEqual(json.loads(self.state_path.read_text()),self.before)
        journal=self.state_path.with_name('paper_rebuild_pending.json')
        original=journal.read_text()
        self.assertTrue(rebuild.recover_rebuild(self.state_path,self.events_path))
        journal.write_text(original)  # Crash after successful writes, before journal removal.
        text=self.events_path.read_text()
        self.assertTrue(rebuild.recover_rebuild(self.state_path,self.events_path))
        self.assertEqual(self.events_path.read_text(),text)

    def test_changed_state_or_corrupt_ledger_never_gets_overwritten(self):
        self.state_path.write_text(json.dumps(dict(self.before,cash_krw=1)))
        with self.assertRaises(RuntimeError):rebuild.commit_rebuild(self.state_path,self.events_path,self.before,self.after,self.events)
        self.state_path.write_text(json.dumps(self.before));self.events_path.write_text('{bad json')
        with self.assertRaises(ValueError):rebuild.commit_rebuild(self.state_path,self.events_path,self.before,self.after,self.events)
        self.assertEqual(json.loads(self.state_path.read_text()),self.before)
        self.assertFalse(self.state_path.with_name('paper_rebuild_pending.json').exists())


class EngineTests(unittest.TestCase):
    def test_full_rebuild_allowed_after_ordinary_rebalance_but_not_repeated(self):
        cfg,state,snap,prices=fixture()
        with patch.object(engine,'CFG',cfg),patch.object(engine,'now_dt',return_value=NOW),patch.object(engine,'load_morning_snapshot',return_value=(snap,ASOF)),patch.object(engine,'get_prices',return_value=prices),patch.object(engine,'commit_rebuild') as commit,patch.object(engine,'telegram'),patch.object(engine,'send_current_status'):
            self.assertTrue(engine.full_rebalance(state,ASOF.isoformat()))
            self.assertEqual(state['last_full_rebuild_vpd_asof'],ASOF.isoformat())
            before=copy.deepcopy(state)
            self.assertFalse(engine.full_rebalance(state,ASOF.isoformat()))
            self.assertFalse(engine.morning_rebalance(state,ASOF.isoformat()))
            self.assertEqual(state,before);commit.assert_called_once()

    def test_failed_preflight_stale_or_old_snapshot_does_not_commit(self):
        cfg,state,snap,prices=fixture();before=copy.deepcopy(state)
        with patch.object(engine,'CFG',cfg),patch.object(engine,'now_dt',return_value=NOW) as clock,patch.object(engine,'load_morning_snapshot',return_value=(snap,ASOF)),patch.object(engine,'get_prices',return_value={}),patch.object(engine,'commit_rebuild') as commit,patch.object(engine,'telegram'):
            self.assertFalse(engine.full_rebalance(state))
            self.assertEqual(state,before)
            clock.return_value=NOW+timedelta(hours=1)
            self.assertFalse(engine.full_rebalance(state))
            clock.return_value=NOW
            state['last_rebalance_vpd_asof']=NOW.isoformat()
            self.assertFalse(engine.full_rebalance(state));commit.assert_not_called()


class ConfirmationTests(unittest.TestCase):
    def test_button_command_and_actor_bound_explicit_confirmation(self):
        self.assertEqual(parse_command('전량 교체'),'rebuild')
        self.assertEqual(parse_command('/rebuild'),'rebuild')
        self.assertTrue(any(b['callback_data']=='nav:rebuild' for row in vpd_keyboard()['inline_keyboard'] for b in row))
        with patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'ALLOWED_USER_IDS',set()),patch.object(server,'CONFIRMATIONS',Confirmations()),patch.object(server,'SCAN_JOB',None),patch.object(server,'ENGINE_JOB',None),patch.object(server,'telegram') as send,patch.object(server,'telegram_api'),patch.object(server,'start_engine',return_value=True) as start:
            callback=lambda data,user='7':dict(id='1',data=data,message={'message_id':1,'chat':{'id':'7'}},**{'from':{'id':user}})
            server.handle_callback(callback('nav:rebuild'))
            start.assert_not_called()
            text,markup=send.call_args.args
            self.assertIn('당일 재진입 제한',text)
            self.assertIn('누적 손익',text)
            button=markup['inline_keyboard'][0][0]
            self.assertEqual(button['text'],'🔴 전량 매도 후 재매수')
            server.handle_callback(callback(button['callback_data'],'8'));start.assert_not_called()
            server.handle_callback(callback(button['callback_data']));start.assert_called_once_with('rebuild')
            server.handle_callback(callback(button['callback_data']));start.assert_called_once()


if __name__=='__main__':unittest.main()
