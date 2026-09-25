"""Verified Drive archival. Failures retain local data; secrets never enter logs."""
import asyncio
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
import aiohttp

LOG = logging.getLogger('magi1.archive')
API = 'https://www.googleapis.com/drive/v3/files'
FIELDS = 'id,name,size,md5Checksum,appProperties,trashed'


def digest(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'md5').hexdigest()


def hour_end(path):
    try:
        return datetime.strptime(path.name[-22:-9], '%Y-%m-%d-%H').replace(tzinfo=timezone.utc).timestamp() + 3600
    except ValueError:
        return None


class Drive:
    def __init__(self, session):
        self.session = session
        self.token = None
        self.expires = 0

    async def request(self, method, url, **kwargs):
        if time.time() >= self.expires:
            data = {k: os.environ['MAGI1_GOOGLE_' + v] for k, v in
                    [('client_id', 'CLIENT_ID'), ('client_secret', 'CLIENT_SECRET'), ('refresh_token', 'REFRESH_TOKEN')]}
            data['grant_type'] = 'refresh_token'
            async with self.session.post('https://oauth2.googleapis.com/token', data=data) as r:
                if r.status != 200:
                    raise RuntimeError('Drive OAuth refresh HTTP %s' % r.status)
                result = await r.json()
                self.token = result['access_token']
                self.expires = time.time() + result.get('expires_in', 3600) - 120
        headers = kwargs.pop('headers', {})
        headers['Authorization'] = 'Bearer ' + self.token
        async with self.session.request(method, url, headers=headers, **kwargs) as r:
            if r.status not in (200, 201, 204):
                raise RuntimeError('Drive %s HTTP %s' % (method, r.status))
            if r.status == 204:
                return {}, dict(r.headers)
            return await r.json(), dict(r.headers)

    async def list(self, query):
        items, page = [], None
        while True:
            params = {'q': query, 'fields': 'nextPageToken,files(' + FIELDS + ')', 'pageSize': '1000'}
            if page:
                params['pageToken'] = page
            data, _ = await self.request('GET', API, params=params)
            items.extend(data.get('files', []))
            page = data.get('nextPageToken')
            if not page:
                return items

    async def folder(self):
        query = "trashed=false and mimeType='application/vnd.google-apps.folder' and appProperties has { key='magi1' and value='archive-v1' }"
        rows = await self.list(query)
        if rows:
            return rows[0]['id']
        row, _ = await self.request('POST', API, json={'name': 'MAGI1-Archive', 'mimeType': 'application/vnd.google-apps.folder', 'appProperties': {'magi1': 'archive-v1'}})
        return row['id']

    async def upload(self, path, folder, props):
        md5 = await asyncio.to_thread(digest, path)
        size = path.stat().st_size
        key = hashlib.sha256((path.name + ':' + md5).encode()).hexdigest()
        rows = await self.list("trashed=false and '%s' in parents and appProperties has { key='content_key' and value='%s' }" % (folder, key))
        for row in rows:
            if row.get('md5Checksum') == md5 and int(row.get('size', -1)) == size:
                return row
        body = {'name': path.name, 'parents': [folder], 'appProperties': {**props, 'content_key': key}}
        # Initiation returns an empty body and a Location header.
        await self.request('GET', API, params={'pageSize': '1', 'fields': 'files(id)'})
        headers = {'Authorization': 'Bearer ' + self.token, 'X-Upload-Content-Type': 'application/gzip', 'X-Upload-Content-Length': str(size)}
        async with self.session.post('https://www.googleapis.com/upload/drive/v3/files', params={'uploadType': 'resumable', 'fields': FIELDS}, json=body, headers=headers) as r:
            if r.status != 200:
                raise RuntimeError('Drive upload initiation HTTP %s' % r.status)
            location = r.headers['Location']
        with path.open('rb') as f:
            row, _ = await self.request('PUT', location, data=f, headers={'Content-Type': 'application/gzip', 'Content-Length': str(size)})
        verified, _ = await self.request('GET', API + '/' + row['id'], params={'fields': FIELDS})
        if verified.get('md5Checksum') != md5 or int(verified.get('size', -1)) != size:
            raise RuntimeError('Drive checksum or size mismatch; source retained')
        return verified


def backup(storage, dest):
    # A separate read connection gives a coherent snapshot without holding the ingestion lock.
    source = sqlite3.connect(storage.root / 'research.db')
    target = sqlite3.connect(dest)
    try:
        source.backup(target, pages=256)
        maximum = target.execute('SELECT COALESCE(MAX(rowid),0) FROM records').fetchone()[0]
    finally:
        source.close()
        target.close()
    with open(dest, 'rb') as src, gzip.open(str(dest) + '.gz', 'wb', compresslevel=6) as out:
        shutil.copyfileobj(src, out, 1024 * 1024)
    dest.unlink()
    return maximum


def recover_archive_workspace(root):
    """Remove abandoned archive scratch databases left by interrupted compaction."""
    work = Path(root) / 'archive_work'
    if not work.exists():
        return {'removed': 0, 'bytes': 0}
    removed = freed = 0
    for path in work.iterdir():
        name = path.name
        scratch = (name.startswith(('old-check.db', 'new-check.db')) or
                   (name.startswith('research-') and ('.db' in name)))
        if scratch and path.is_file():
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            removed += 1; freed += size
    LOG.info('archive_workspace_recovered removed=%d freed_bytes=%d', removed, freed)
    return {'removed': removed, 'bytes': freed}


def prune_research(storage, max_rowid, cutoff_ms):
    with storage._lock:
        storage.db.execute('DELETE FROM records WHERE rowid<=? AND ts_ms>0 AND ts_ms<?', (max_rowid, cutoff_ms))
        storage.db.commit()


class Archiver:
    def __init__(self, storage):
        self.storage = storage
        self.started = time.time()
        self.needs_compaction = True
        self.state = storage.restore('drive_archive_v1', {'files': {}})

    async def cycle(self, drive):
        folder = await drive.folder()
        now = time.time()
        # Ingestion finishes and checkpoints closed-hour analysis before declaring coverage.
        coverage = self.storage.restore('analysis_coverage_v1', {})
        candidates = [p for p in sorted(self.storage.raw.glob('*.jsonl.gz'))
                      if hour_end(p) is not None and hour_end(p) < now - 3600 and p.stat().st_mtime < now - 300]
        pending = [p for p in candidates if p.name not in self.state['files']][:12]
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if pending or self.state.get('research_day') != day or self.needs_compaction:
            tmp = self.storage.root / 'archive_work'
            tmp.mkdir(exist_ok=True)
            dest = tmp / ('research-%s.db' % time.time_ns())
            maximum = await asyncio.to_thread(backup, self.storage, dest)
            compressed = Path(str(dest) + '.gz')
            try:
                proof = await drive.upload(compressed, folder, {'kind': 'research', 'permanent': 'true'})
                if hasattr(drive,'session'):
                    from .compact import compact_backups
                    result=await compact_backups(drive,folder,proof,compressed,tmp)
                    self.storage.checkpoint('archive_compaction_latest',result)
                self.needs_compaction=False
            finally:
                compressed.unlink(missing_ok=True)
            self.state['research_day'] = day
            self.state['research_id'] = proof['id']
            self.storage.checkpoint('drive_archive_v1', self.state)
            await asyncio.to_thread(prune_research, self.storage, maximum, int((now - 30 * 86400) * 1000))
            LOG.info('archive_research_verified id=%s', proof['id'])
        for p in pending:
            end = hour_end(p)
            covered = coverage.get('start', now) <= end - 3600 and coverage.get('through', 0) >= end + 3600
            props = {'kind': 'raw', 'end': str(int(end)), 'analysis_verified': str(covered).lower(), 'research_id': self.state['research_id']}
            row = await drive.upload(p, folder, props)
            self.state['files'][p.name] = {'id': row['id'], 'md5': row['md5Checksum'], 'size': int(row['size'])}
            self.storage.checkpoint('drive_archive_v1', self.state)
            LOG.info('archive_raw_verified name=%s bytes=%s analysis_verified=%s', p.name, row['size'], covered)
        # Only remove a local copy after re-verifying its actual remote object.
        for p in candidates:
            saved = self.state['files'].get(p.name)
            if not saved:
                continue
            remote, _ = await drive.request('GET', API + '/' + saved['id'], params={'fields': FIELDS})
            if not remote.get('trashed') and remote.get('md5Checksum') == await asyncio.to_thread(digest, p) and int(remote.get('size', -1)) == p.stat().st_size:
                p.unlink()
        # Scope permanent deletion strictly to our own tagged raw files with analysis proof.
        rows = await drive.list("trashed=false and '%s' in parents and appProperties has { key='kind' and value='raw' }" % folder)
        for row in rows:
            props = row.get('appProperties', {})
            if props.get('analysis_verified') != 'true' or int(props.get('end', now)) >= now - 7 * 86400:
                continue
            proof, _ = await drive.request('GET', API + '/' + props['research_id'], params={'fields': FIELDS})
            if proof.get('trashed') or not proof.get('md5Checksum'):
                continue
            await drive.request('DELETE', API + '/' + row['id'])
            LOG.info('archive_raw_expired id=%s', row['id'])
        self.state['files'] = {name: value for name, value in self.state['files'].items() if (self.storage.raw / name).exists()}
        self.storage.checkpoint('drive_archive_v1', self.state)
        LOG.info('archive_cycle_ok folder=%s local_closed_remaining=%d', folder, sum(p.exists() for p in candidates))

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            drive = Drive(session)
            while True:
                try:
                    await self.cycle(drive)
                except Exception as exc:
                    # Never stringify arbitrary HTTP errors which might include tokenized URLs.
                    message = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('Drive ') else type(exc).__name__
                    LOG.error('archive_cycle_failed reason=%s local_data_retained=true', message)
                await asyncio.sleep(300)
