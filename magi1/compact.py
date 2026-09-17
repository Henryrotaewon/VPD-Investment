"""Remove only provably superseded research snapshots, preserving raw-file references."""
import asyncio
import gzip
import hashlib
import json
import logging
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

LOG=logging.getLogger('magi1.archive')


def meaningful_rows(compressed, output):
    with gzip.open(compressed,'rb') as src, open(output,'wb') as dest:
        shutil.copyfileobj(src,dest,1024*1024)
    db=sqlite3.connect('file:'+str(output)+'?mode=ro',uri=True)
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise RuntimeError('Drive invalid research snapshot')
        rows=Counter()
        for kind,ts,asset,payload in db.execute('SELECT kind,ts_ms,asset,payload FROM records'):
            if kind=='propagation_missing':continue
            if kind=='evaluation' and json.loads(payload).get('status')=='MISSING_DATA':continue
            value=json.dumps([kind,ts,asset,payload],separators=(',',':')).encode()
            rows[hashlib.sha256(value).digest()]+=1
        return rows
    finally:
        db.close();Path(output).unlink(missing_ok=True)


async def compact_backups(drive, folder, proof, current, workdir):
    from .archive import API,FIELDS,digest
    # Comparing actual row multisets prevents loss of older unique analytical history.
    newer=await asyncio.to_thread(meaningful_rows,current,workdir/'new-check.db')
    snapshots=await drive.list("trashed=false and '%s' in parents and appProperties has { key='kind' and value='research' }" % folder)
    raw=await drive.list("trashed=false and '%s' in parents and appProperties has { key='kind' and value='raw' }" % folder)
    deleted=0; freed=0
    for row in snapshots:
        if row['id']==proof['id'] or not row.get('name','').startswith('research-'):
            continue
        path=workdir/'old-check.db.gz'
        try:
            # Auth token has been refreshed by list(). Download only this app-owned archive.
            async with drive.session.get(API+'/'+row['id'],params={'alt':'media'},headers={'Authorization':'Bearer '+drive.token}) as response:
                if response.status!=200:raise RuntimeError('Drive snapshot download HTTP %s'%response.status)
                with path.open('wb') as out:
                    async for chunk in response.content.iter_chunked(1024*1024):out.write(chunk)
            if path.stat().st_size!=int(row.get('size',-1)) or await asyncio.to_thread(digest,path)!=row.get('md5Checksum'):
                raise RuntimeError('Drive snapshot download checksum mismatch')
            older=await asyncio.to_thread(meaningful_rows,path,workdir/'old-check.db')
            if not older<=newer:
                LOG.info('archive_snapshot_retained id=%s reason=unique_rows',row['id']);continue
            check,_=await drive.request('GET',API+'/'+proof['id'],params={'fields':FIELDS})
            if check.get('trashed') or check.get('md5Checksum')!=proof['md5Checksum']:
                raise RuntimeError('Drive replacement snapshot unavailable')
            for item in raw:
                props=item.get('appProperties',{})
                if props.get('research_id')!=row['id']:continue
                props={**props,'research_id':proof['id']}
                await drive.request('PATCH',API+'/'+item['id'],json={'appProperties':props})
                verified,_=await drive.request('GET',API+'/'+item['id'],params={'fields':FIELDS})
                if verified.get('appProperties',{}).get('research_id')!=proof['id']:
                    raise RuntimeError('Drive raw proof relink failed')
                item['appProperties']=props
            await drive.request('DELETE',API+'/'+row['id'])
            deleted+=1;freed+=int(row['size'])
            LOG.info('archive_snapshot_deleted id=%s bytes=%s replacement=%s',row['id'],row['size'],proof['id'])
        finally:path.unlink(missing_ok=True)
    LOG.info('archive_compaction_complete deleted=%s freed_bytes=%s',deleted,freed)
    return {'deleted':deleted,'freed_bytes':freed}
