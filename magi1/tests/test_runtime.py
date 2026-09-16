import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from magi1 import runner
from magi1.storage import Storage
from magi1.collectors.public_ws import Adapter


class RuntimeTests(unittest.TestCase):
    def test_bithumb_book_microseconds_upbit_milliseconds(self):
        row={'type':'orderbook','code':'KRW-BTC','timestamp':1789524249030123,
             'orderbook_units':[{'bid_price':100,'bid_size':1,'ask_price':101,'ask_size':2}]}
        event=Adapter('bithumb').parse(row,1789524249200)[0]
        self.assertEqual(event.exchange_ts_ms,1789524249030)
        row['timestamp']=1789524249030
        self.assertEqual(Adapter('upbit').parse(row,1789524249200)[0].exchange_ts_ms,1789524249030)

    def test_module_entrypoint(self):
        result = subprocess.run([sys.executable, '-m', 'magi1.runner', '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--duration', result.stdout)

    def test_start_discovers_runs_and_closes(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'MAGI1_DATA_DIR': root, 'MAGI1_MODE': 'COLLECT_ONLY'}, clear=True):
                with patch.object(runner, 'discover', AsyncMock(return_value={'assets': ['BTC']})) as discover:
                    with patch.object(runner.App, 'run', AsyncMock()) as run:
                        asyncio.run(runner.start(1))
                        discover.assert_awaited_once()
                        run.assert_awaited_once_with(1)
            storage = Storage(root)
            self.assertEqual(storage.summary()['research_records']['universe']['count'], 1)
            self.assertIsNotNone(storage.restore('pending'))
            storage.close()

    def test_railway_requires_volume(self):
        with patch.dict(os.environ, {'RAILWAY_ENVIRONMENT_ID': 'test', 'MAGI1_DATA_DIR': '/data/magi1'}, clear=True):
            with patch.object(os.path, 'ismount', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'mounted /data'):
                    runner.data_root()

    def test_counts_survive_restart_without_double_flush(self):
        with tempfile.TemporaryDirectory() as root:
            s = Storage(root)
            s.append_raw('trade', {'received_ts_ms': 1000})
            s.append_raw('book', {'received_ts_ms': 2000})
            s.append('evaluation', {'event_ts_ms': 3000})
            first = s.summary()
            self.assertEqual(first['raw_write_stats']['kinds']['trade']['count'], 1)
            s.close()
            s = Storage(root)
            s.append_raw('trade', {'received_ts_ms': 4000})
            second = s.summary()
            self.assertEqual(second['raw_write_stats']['kinds']['trade'], {'count': 2, 'last_ts_ms': 4000})
            self.assertEqual(second['research_total'], 1)
            self.assertEqual(s.summary()['raw_write_stats'], second['raw_write_stats'])
            s.close()
