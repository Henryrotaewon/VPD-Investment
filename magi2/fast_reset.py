"""Explicit user-authorized one-time reset, before any FAST workers start."""
from pathlib import Path
import json
import time

RESET_ID='fast-current-fivepct-v4-20260921'


def reset_once(root,log):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    marker=root/'fast_reset.json'
    if marker.exists() and json.loads(marker.read_text()).get('reset_id')==RESET_ID:return False
    # Exact allowlist: no VPD, MAGI1, MAGI3, secrets or unrelated volume files.
    for name in ('fast_paper.sqlite3','fast_evidence.sqlite3'):
        for suffix in ('','-wal','-shm'):
            (root/(name+suffix)).unlink(missing_ok=True)
    temp=marker.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(reset_id=RESET_ID,reset_ms=time.time_ns()//1000000)))
    temp.replace(marker)
    log('fast_reset_completed reset_id='+RESET_ID+' captures_and_paper_deleted=true seed_krw_each=1000000')
    return True
