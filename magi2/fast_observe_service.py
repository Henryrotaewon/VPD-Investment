"""Supervise isolated PUBLIC FAST workers and expose read-only Telegram status."""
import atexit
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]


class Service:
    def __init__(self,state_dir,log):
        self.directory=Path(state_dir)/'fast-observe';self.directory.mkdir(parents=True,exist_ok=True)
        self.db=self.directory/'observe.sqlite3';self.status=self.directory/'status.json'
        self.log=log;self.child=None;self.stopped=threading.Event();self.data={}
        atexit.register(self.stop)
    def save(self,**values):
        self.data.update(values,updated_ms=int(time.time()*1000))
        temp=self.status.with_suffix('.tmp');temp.write_text(json.dumps(self.data));temp.replace(self.status)
    def stop(self):
        self.stopped.set()
        if self.child and self.child.poll() is None:self.child.terminate()
    def worker(self,module,*args):
        env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ','SSL_CERT_FILE','SSL_CERT_DIR','HOME','PYTHONPATH')}
        env['PYTHONUNBUFFERED']='1'
        self.child=subprocess.Popen([sys.executable,'-m',module,'--db',str(self.db),*args],cwd=ROOT,
                                    env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        for line in self.child.stdout:
            if self.stopped.is_set():break
            try:
                value=json.loads(line)
                if value.get('mode')=='BOOTSTRAP_NO_ORDERS':self.save(cutoff_ms=value['cutoff_ms'],done=0,complete=0,incomplete=0)
                elif 'missing_buckets' in value:
                    self.save(done=self.data.get('done',0)+1,complete=self.data.get('complete',0)+(value['missing_buckets']==0),
                              incomplete=self.data.get('incomplete',0)+(value['missing_buckets']>0),last_market=value['symbol'])
                elif 'market_count' in value:self.save(total=value['market_count'])
                elif value.get('mode')=='PUBLIC_OBSERVE_NO_ORDERS':self.save(observation=value)
                else:self.save(last_progress=value)
                self.log('fast_observe '+json.dumps(value))
            except (ValueError,TypeError):
                self.log('fast_observe_worker '+line.strip()[:300])
        code=self.child.wait();self.child=None
        return code
    def run(self):
        while not self.stopped.is_set():
            try:
                self.save(phase='REPAIRING',detail='초기 적재 또는 누락 보완 중 · 포착 중지')
                code=self.worker('magi1.fast_bootstrap')
                if code:raise RuntimeError('BOOTSTRAP_EXIT_'+str(code))
                if self.stopped.is_set():break
                self.save(phase='OBSERVING',detail='수급 확인 · 주문 없음')
                code=self.worker('magi1.fast_observe','--duration','1800')
                if code:raise RuntimeError('OBSERVE_EXIT_'+str(code))
                # Bound missed-candle repair and historical retention; intentionally
                # stop captures while native REST history is repaired.
            except Exception as exc:
                self.save(phase='RETRY_WAIT',detail=type(exc).__name__+': '+str(exc))
                self.log('fast_observe_retry '+str(exc))
                if self.stopped.wait(60):break
    def start(self):
        threading.Thread(target=self.run,name='fast-observe-supervisor',daemon=True).start()
        return self


def view(state_dir):
    directory=Path(state_dir)/'fast-observe'
    try:data=json.loads((directory/'status.json').read_text())
    except (OSError,ValueError):return '⚡ FAST 포착·추적\n초기화 대기 · 주문 없음'
    age=max(0,int(time.time()*1000)-data['updated_ms'])//1000
    phase={'REPAIRING':'기준자료 적재·누락 보완','OBSERVING':'포착·추적 중','RETRY_WAIT':'오류 후 재시도 대기'}.get(data['phase'],data['phase'])
    lines=['⚡ FAST 포착·추적 · 주문 없음',f'상태: {phase} · 갱신 {age}초 전']
    if age>180:lines.append('⚠ 상태 갱신 지연 · 가동 여부 확인 필요')
    done,total=data.get('done',0),data.get('total',0)
    lines.append(f'기준자료 검사 {done}/{total or "확인 중"}종목 · 완비 {data.get("complete",0)} · 미완비 {data.get("incomplete",0)}')
    lines.append('최근 24시간 상세봉 + 직전 10일 동일시간 거래대금')
    try:
        db=sqlite3.connect('file:'+str(directory/'observe.sqlite3')+'?mode=ro',uri=True,timeout=1)
        count=db.execute('SELECT count(*) FROM captures').fetchone()[0]
        lines.append(f'누적 포착 {count}건')
        for ident,stamp,symbol,price in db.execute('SELECT id,ts,symbol,price FROM captures ORDER BY ts DESC LIMIT 5'):
            clock=datetime.fromtimestamp(stamp/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')
            lines.append(f'{symbol} · {clock} · 기준 {price:g}원')
            marks=db.execute('SELECT horizon,status,return_pct FROM outcomes WHERE capture_id=? ORDER BY horizon',(ident,)).fetchall()
            lines.append(' / '.join(f'{h//60}분 '+(f'{ret:+.2f}%' if state=='OBSERVED' else '자료 누락') for h,state,ret in marks) or '가격 추적 대기')
        gaps=db.execute("SELECT count(*) FROM events WHERE kind IN ('GAP','PROCESS_GAP') AND ts>?",(int(time.time()*1000)-86400000,)).fetchone()[0]
        missing=db.execute("SELECT count(*) FROM outcomes WHERE status='MISSING'").fetchone()[0]
        lines.append(f'24시간 연결·처리지연 {gaps}건 · 누적 추적 누락 {missing}건')
        db.close()
    except sqlite3.Error:lines.append('원장 조회 대기 · 잠시 후 다시 조회')
    lines.append('가격변화 관측값 · 체결수익률 아님\n30분 관측 후 누락 보완 동안 포착을 잠시 중지합니다.')
    return '\n'.join(lines)
