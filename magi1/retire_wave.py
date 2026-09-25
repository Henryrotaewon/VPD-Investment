"""One-time user-authorized removal of retired WAVE analysis, not market/VPD data."""
import json
from pathlib import Path

MIGRATION='retire-wave-analysis-20260925-v1'
KINDS=('flow_state','shock_chain','propagation','propagation_missing','reception_trial_v3')
FLOW_COHORTS=('FLOW_ONLY','FLOW_VPD','FLOW_VPD_NON_REACTION')


def retire(storage, now_ms):
    with storage._lock:
        if storage.restore(MIGRATION): return storage.restore(MIGRATION)
        counts={}
        with storage.db:
            for kind in KINDS:
                counts[kind]=storage.db.execute('DELETE FROM records WHERE kind=?',(kind,)).rowcount
            counts['flow_evaluations']=storage.db.execute(
                "DELETE FROM records WHERE kind='evaluation' AND json_valid(payload) "
                "AND json_extract(payload,'$.cohort') IN (?,?,?)",FLOW_COHORTS).rowcount
            # Preserve unknown schemas and any entry containing the independent VPD cohort.
            counts['flow_entries']=storage.db.execute(
                "DELETE FROM records WHERE kind='cohort_entry' AND json_valid(payload) "
                "AND EXISTS (SELECT 1 FROM json_each(records.payload,'$.cohorts') WHERE value IN (?,?,?)) "
                "AND NOT EXISTS (SELECT 1 FROM json_each(records.payload,'$.cohorts') WHERE value='VPD_ONLY')",
                FLOW_COHORTS).rowcount
            pending=storage.restore('pending',{})
            pending={k:v for k,v in pending.items() if 'VPD_ONLY' in v.get('cohorts',[]) or
                     not set(v.get('cohorts',[])).intersection(FLOW_COHORTS)}
            storage.db.execute('UPDATE checkpoints SET payload=? WHERE key=?',(json.dumps(pending),'pending'))
            storage.db.execute('DELETE FROM checkpoints WHERE key=?',('analysis_summary_v1',))
        # Exact generated-file allowlist. Never recursively delete reports, DBs or archives.
        removed=[]
        for name in ('wave_latest.json','wave_latest.tmp'):
            path=storage.root/'exports'/name
            if path.is_file() and not path.is_symlink():
                path.unlink();removed.append(str(path.relative_to(storage.root)))
        # Existing daily markdown combines WAVE and VPD. Retain only its VPD outcome table.
        sanitized=[]
        for path in sorted((storage.root/'reports').glob('magi1_daily_*.md')):
            if path.is_symlink(): continue
            text=path.read_text(encoding='utf-8')
            if not text.startswith('# MAGI1 Daily Crypto Shock Report'):continue
            lines=text.splitlines()
            kept=[line for line in lines if line.startswith(('Window:', '| VPD_ONLY |'))]
            headers=['# MAGI1 retained VPD observations', '',
                     'WAVE analysis retired; VPD observations retained. These are research outcomes, not trades.', '',
                     '| Cohort | Direction | Horizon | Valid observations | Mean return | Win rate | Mean MFE | Mean MAE |',
                     '|---|---|---:|---:|---:|---:|---:|---:|']
            temp=path.with_suffix('.tmp');temp.write_text('\n'.join(headers+kept)+'\n',encoding='utf-8');temp.replace(path)
            sanitized.append(path.name)
        result=dict(migration=MIGRATION,at_ms=now_ms,deleted_records=counts,
                    removed_files=removed,sanitized_mixed_reports=sanitized,
                    raw_preserved=True,vpd_preserved=True,drive_mixed_backups_preserved=True)
        storage.checkpoint(MIGRATION,result)
        return result
