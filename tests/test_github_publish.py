import base64
import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scanner'))
from github_publish import publish_bundle, StaleSnapshotError


def snap(day, **extra):
    return json.dumps(dict(asof=f'2026-09-{day:02d}T07:25:56+09:00', top10=[], **extra))


class Response:
    def __init__(self, code, body=None):
        self.status_code, self.body = code, body
    def json(self):
        return self.body


class GitHub:
    def __init__(self, files, conflict=None, code=409):
        self.files = dict(files)
        self.head = 'h0'
        self.conflict = conflict
        self.code = code
        self.patches = 0
        self.trees = {}
        self.commits = {}
        self.reads = []
    def get(self, url, **kw):
        if '/git/ref/heads/' in url:
            return Response(200, {'object': {'sha': self.head}})
        if '/git/commits/' in url:
            return Response(200, {'tree': {'sha': 'base-tree'}})
        path = url.split('/contents/')[1]
        self.reads.append((path, kw['params']['ref']))
        if path not in self.files:
            return Response(404)
        return Response(200, {'sha': 'blob', 'encoding': 'base64',
                             'content': base64.b64encode(self.files[path].encode()).decode()})
    def post(self, url, json, **kw):
        if url.endswith('/git/trees'):
            key = f't{len(self.trees)}'
            self.trees[key] = json
        else:
            key = f'c{len(self.commits)}'
            self.commits[key] = json
        return Response(201, {'sha': key})
    def patch(self, url, json, **kw):
        self.patches += 1
        assert json['force'] is False
        if self.patches == 1 and self.conflict is not None:
            self.files.update(self.conflict)
            self.head = 'h1'
            return Response(self.code)
        commit = self.commits[json['sha']]
        assert commit['parents'] == [self.head]
        self.files.update({x['path']: x['content'] for x in self.trees[commit['tree']]['tree']})
        self.head = json['sha']
        return Response(200)


class PublishTests(unittest.TestCase):
    def files(self):
        return {'data/vpd_latest.json': snap(17, wpi5_shadow={}),
                'data/magi1_upbit_morning_state.json': snap(17),
                'data/vpd_all_latest.csv': 'coin,VPD\nA,80\n',
                'data/vpd_history.csv': 'scan_time,coin,VPD\n17,A,80\n'}
    def publish(self, gh, **kw):
        return publish_bundle(self.files(), 'test', client=gh, sleep=lambda _: None, **kw)
    def test_atomic_success_and_remote_history_preserved(self):
        gh = GitHub({'data/vpd_latest.json': snap(16), 'data/vpd_history.csv': 'scan_time,coin,VPD\n16,B,70\n'})
        self.publish(gh)
        self.assertEqual(gh.patches, 1)
        self.assertIn('16,B,70', gh.files['data/vpd_history.csv'])
        self.assertIn('17,A,80', gh.files['data/vpd_history.csv'])
        self.assertTrue(all(ref == 'h0' for _, ref in gh.reads))
        self.assertEqual(len(gh.trees['t0']['tree']), 4)
    def test_unrelated_branch_conflict_retries_409_and_422(self):
        for code in (409, 422):
            gh = GitHub({'data/vpd_latest.json': snap(16)}, {'data/watchlist.json': 'new'}, code)
            self.publish(gh)
            self.assertEqual(gh.patches, 2)
            self.assertEqual(gh.files['data/watchlist.json'], 'new')
            self.assertEqual(gh.commits['c1']['parents'], ['h1'])
    def test_newer_snapshot_after_conflict_is_not_overwritten(self):
        gh = GitHub({'data/vpd_latest.json': snap(16)}, {'data/vpd_latest.json': snap(18)})
        with self.assertRaises(StaleSnapshotError): self.publish(gh)
        self.assertEqual(gh.patches, 1)
        self.assertNotIn('data/magi1_upbit_morning_state.json', gh.files)
        self.assertEqual(gh.files['data/vpd_latest.json'], snap(18))
    def test_newer_session_blocks_whole_batch(self):
        gh = GitHub({'data/vpd_latest.json': snap(16), 'data/magi1_upbit_morning_state.json': snap(18)})
        with self.assertRaises(StaleSnapshotError): self.publish(gh)
        self.assertEqual(gh.patches, 0)
    def test_malformed_current_timestamp_fails_closed(self):
        gh = GitHub({'data/vpd_latest.json': '{}'})
        with self.assertRaises(ValueError): self.publish(gh)
        self.assertEqual(gh.patches, 0)
    def test_exhaustion_does_not_publish_partial_data(self):
        gh = GitHub({'data/vpd_latest.json': snap(16)}, {'data/watchlist.json': 'new'})
        with self.assertRaisesRegex(RuntimeError, 'exhausted'): self.publish(gh, attempts=1)
        self.assertEqual(gh.files['data/vpd_latest.json'], snap(16))
    def test_equal_time_base_cannot_remove_enrichment(self):
        gh = GitHub({'data/vpd_latest.json': snap(17, wpi5_shadow={}, v1_6_layers={})})
        with self.assertRaises(StaleSnapshotError): self.publish(gh)
        self.assertEqual(gh.patches, 0)
    def test_non_contention_422_is_not_retried(self):
        gh = GitHub({'data/vpd_latest.json': snap(16)})
        gh.patch = lambda *a, **kw: Response(422)
        with self.assertRaisesRegex(RuntimeError, 'rejected'): self.publish(gh)
        self.assertEqual(len(gh.commits), 1)

if __name__ == '__main__': unittest.main()
