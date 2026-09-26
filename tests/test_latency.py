import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from magi2.latency import TimedExecutor, span, trace
from magi2.shadow_bridge import candidates, source_error_reason

class LatencyTests(unittest.TestCase):
    def test_failure_duration_without_secret(self):
        output=io.StringIO()
        with redirect_stdout(output), patch('magi2.latency.time.monotonic', side_effect=[1, 1.125]):
            with self.assertRaises(ValueError):
                with span('fetch'):
                    raise ValueError('private-token-url')
        line=output.getvalue()
        self.assertNotIn('private-token', line)
        row=json.loads(line.removeprefix('latency '))
        self.assertEqual(row['elapsed_ms'],125)
        self.assertEqual(row['outcome'],'error')

    def test_worker_trace_propagates_and_resets(self):
        output=io.StringIO()
        with redirect_stdout(output), TimedExecutor(max_workers=1,thread_name_prefix='test') as pool:
            token=trace.set('request-one')
            try: self.assertEqual(pool.submit(trace.get).result(),'request-one')
            finally: trace.reset(token)
            self.assertEqual(pool.submit(trace.get).result(),'background')
        rows=[json.loads(x.removeprefix('latency ')) for x in output.getvalue().splitlines()]
        self.assertEqual([r['stage'] for r in rows],['test.queue','test.run']*2)

    def test_shadow_expiry_stays_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'STALE_VPD_SNAPSHOT'):
            candidates({'universe':'UPBIT_KRW','asof':'2026-09-25T16:12:52+09:00'},1790425000000)
        self.assertEqual(source_error_reason(ValueError('STALE_VPD_SNAPSHOT')),'STALE_VPD_SNAPSHOT')
        self.assertEqual(source_error_reason(ValueError('https://secret')),'SOURCE_VALIDATION_OR_FETCH_FAILED')
