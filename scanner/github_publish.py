"""Publish a complete scan with optimistic, non-forced branch updates."""
import base64
import csv
import io
import json
import time
from datetime import datetime, timezone, timedelta

import requests


class StaleSnapshotError(RuntimeError):
    """The repository already contains a newer scan."""


def snapshot_time(text):
    data = json.loads(text)
    value = data.get('asof') or data.get('asof_kst')
    if not value:
        raise ValueError('Snapshot timestamp is missing')
    stamp = datetime.fromisoformat(value.replace(' KST', '+09:00').replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone(timedelta(hours=9)))
    return stamp


def merge_history(remote, incoming):
    """Retain remote rows, including commits made while this scan ran."""
    readers = [csv.DictReader(io.StringIO(s.lstrip('\ufeff'))) for s in (remote, incoming) if s]
    fields, rows = [], {}
    for reader in readers:
        if not {'scan_time', 'coin'} <= set(reader.fieldnames or []):
            raise ValueError('Invalid history columns')
        for field in reader.fieldnames:
            if field not in fields:
                fields.append(field)
        for row in reader:
            if not row.get('scan_time') or not row.get('coin'):
                raise ValueError('Invalid history key')
            rows.setdefault((row['scan_time'], row['coin']), row)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows.values())
    return out.getvalue()


def publish_bundle(files, token, *, repo='Henryrotaewon/VPD-Investment', branch='main',
                   client=requests, sleep=time.sleep, attempts=5):
    """Rebuild on the newest head after contention; never force or regress time.

    Latest JSON, CSV, history, enrichment and session become visible in one commit.
    A failed attempt creates only unreachable Git objects, never partial live data.
    """
    if attempts < 1:
        raise ValueError('attempts must be positive')
    candidate = dict(files)
    stamp = snapshot_time(candidate['data/vpd_latest.json'])
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}
    root = f'https://api.github.com/repos/{repo}'

    def call(method, path, **kwargs):
        response = getattr(client, method)(root + path, headers=headers, timeout=60, **kwargs)
        if response.status_code not in (200, 201):
            raise RuntimeError(f'GitHub {method} {path}: HTTP {response.status_code}')
        return response.json()

    def read(path, head):
        response = client.get(root + '/contents/' + path, headers=headers,
                              params={'ref': head}, timeout=60)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f'GitHub read {path}: HTTP {response.status_code}')
        meta = response.json()
        if meta.get('encoding') != 'base64':
            meta = call('get', '/git/blobs/' + meta['sha'])
        return base64.b64decode(meta['content']).decode('utf-8-sig')

    for attempt in range(attempts):
        head = call('get', '/git/ref/heads/' + branch)['object']['sha']
        tree = call('get', '/git/commits/' + head)['tree']['sha']
        current = read('data/vpd_latest.json', head)
        if current is not None:
            old_stamp = snapshot_time(current)  # Unknown time fails closed.
            if old_stamp > stamp:
                raise StaleSnapshotError('Newer VPD snapshot exists; publication stopped')
            if old_stamp == stamp:
                existing, proposed = json.loads(current), json.loads(candidate['data/vpd_latest.json'])
                # Equal-time publication may enrich a base scan, never remove enrichment.
                for key in ('wpi5_shadow', 'wpi5_replay_30d', 'v1_6_layers'):
                    if key in existing and key not in proposed:
                        raise StaleSnapshotError('Equal-time publication would remove enrichment')
        for path, text in candidate.items():
            if path.startswith('data/magi1_upbit_') and path.endswith('_state.json'):
                session_current = read(path, head)
                if session_current is not None and snapshot_time(session_current) > snapshot_time(text):
                    raise StaleSnapshotError('Newer session snapshot exists; publication stopped')
        batch = dict(candidate)
        hist_path = 'data/vpd_history.csv'
        if hist_path in batch:
            batch[hist_path] = merge_history(read(hist_path, head), batch[hist_path])
        entries = [{'path': p, 'mode': '100644', 'type': 'blob', 'content': text}
                   for p, text in batch.items()]
        new_tree = call('post', '/git/trees', json={'base_tree': tree, 'tree': entries})['sha']
        commit = call('post', '/git/commits', json={
            'message': f'Publish MAGI1 scan {stamp.isoformat()}',
            'tree': new_tree, 'parents': [head]})['sha']
        response = client.patch(root + '/git/refs/heads/' + branch, headers=headers,
                                json={'sha': commit, 'force': False}, timeout=60)
        if response.status_code == 200:
            print(f'MAGI1 scan bundle published: {commit}')
            return commit
        if response.status_code not in (409, 422):
            raise RuntimeError(f'GitHub publish: HTTP {response.status_code}')
        # 422 can also mean protected branch/validation error, not contention.
        latest_head = call('get', '/git/ref/heads/' + branch)['object']['sha']
        if latest_head == head:
            raise RuntimeError(f'GitHub rejected publication: HTTP {response.status_code}')
        if attempt + 1 < attempts:
            print(f'MAGI1 publish contention; retry {attempt + 1}/{attempts - 1}')
            sleep(min(2 ** attempt, 8))
    raise RuntimeError('GitHub publication contention: retries exhausted')
