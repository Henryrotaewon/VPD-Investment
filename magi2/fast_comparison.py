"""Comparable venue-level detection statistics; never a trade profitability report."""
from collections import defaultdict
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

HORIZON_MS=300000
TOLERANCE_MS=5000
VERSION='forward-5m-bid-v1'
VENUES=('upbit','bithumb','binance','kraken')

class ForwardCheck:
    def __init__(self,signal_ts,entry_ask):
        self.signal_ts=signal_ts;self.ask=entry_ask;self.done=False
    def quote(self,ts,bid):
        if self.done or ts<self.signal_ts+HORIZON_MS:return None
        if not math.isfinite(bid) or bid<=0:return None
        self.done=True
        if ts>self.signal_ts+HORIZON_MS+TOLERANCE_MS:
            return {'status':'UNAVAILABLE','reason':'MISSED_EVALUATION_WINDOW','due_ts_ms':self.signal_ts+HORIZON_MS}
        value=(bid/self.ask-1)*10000
        return {'status':'EVALUATED','signal_ts_ms':self.signal_ts,'due_ts_ms':self.signal_ts+HORIZON_MS,
                'observed_ts_ms':ts,'entry_ask':self.ask,'exit_bid':bid,'mark_return_bps':value,
                'non_rising':value<=0,'fees_included':False}

def aggregate(events,since,until,selection_rule='fast-auto-v1'):
    signals={};outcomes={};scan_keys=set();scans=defaultdict(lambda:{'scans':0,'ranked':0})
    for e in events:
        if e['ts_ms']>until:continue
        if e['event_type']=='SIGNAL_DETECTED' and since<=e['ts_ms']<=until and e.get('rule_version')==selection_rule:
            signals.setdefault(e['signal_id'],e)
        elif e['event_type']=='SIGNAL_EVALUATED' and e.get('rule_version')==VERSION:
            outcomes.setdefault(e['signal_id'],e.get('result',{}))
        elif e['event_type']=='SCAN_COMPLETED' and since<=e['ts_ms']<=until and e.get('rule_version')==selection_rule:
            obs=e.get('observed',{})
            key=(e['venue'],e['ts_ms'])
            if obs.get('status')=='RANKED' and key not in scan_keys:
                scan_keys.add(key)
                scans[e['venue']]['scans']+=1;scans[e['venue']]['ranked']+=obs['ranked']
    result=[]
    for venue in VENUES:
        selected=[s for s in signals.values() if s['venue']==venue]
        row={'venue':venue,'signals':len(selected),'evaluated':0,'non_rising':0,'pending':0,'unavailable':0,**scans[venue]}
        for s in selected:
            o=outcomes.get(s['signal_id'])
            if o and o.get('status')=='EVALUATED':
                row['evaluated']+=1;row['non_rising']+=int(o['non_rising'])
            elif o and o.get('status')=='UNAVAILABLE':row['unavailable']+=1
            elif until<s['ts_ms']+HORIZON_MS+TOLERANCE_MS:row['pending']+=1
            else:row['unavailable']+=1
        row['non_rising_pct']=row['non_rising']/row['evaluated']*100 if row['evaluated'] else None
        row['frequency_signals']=sum((venue,s.get('observed',{}).get('selection_scan_ts_ms')) in scan_keys for s in selected)
        row['signals_per_1000_symbol_scans']=row['frequency_signals']/row['ranked']*1000 if row['ranked'] else None
        result.append(row)
    return result

def report(audit,now_ms):
    since=now_ms-72*3600000
    # Short consistent DB snapshot, then aggregate/render without blocking writes.
    with audit.lock:
        rows=audit.db.execute("SELECT ts_ms,signal_id,event_type,venue,payload FROM fast_events WHERE ts_ms>=? AND ts_ms<=? AND event_type IN ('SIGNAL_DETECTED','SIGNAL_EVALUATED','SCAN_COMPLETED') ORDER BY ts_ms,rowid",(since,now_ms)).fetchall()
    events=[dict(ts_ms=r[0],signal_id=r[1],event_type=r[2],venue=r[3],**json.loads(r[4])) for r in rows]
    def date(ts):return datetime.fromtimestamp(ts/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M')
    lines=['📊 FAST 거래소별 검증 비교',f'{date(since)} ~ {date(now_ms)} KST · 최근 72시간',
           '동일 기준: 포착 당시 매도호가 → 5분 뒤 매수호가. 비상승(≤0%)을 오탐 대용으로 집계합니다. 수수료 미차감·실매매 성적 아님.','']
    for rule,label in [('fast-volume-accel-v2','v2 거래대금·가격 가속'),('fast-auto-v1','v1 이전 돌파 조건')]:
        lines.append(label)
        for r in aggregate(events,since,now_ms,rule):
            rate='—' if r['non_rising_pct'] is None else f"{r['non_rising_pct']:.1f}% ({r['non_rising']}/{r['evaluated']})"
            lines.extend([f"{r['venue'].upper()} · 신호 {r['signals']}건 / 탐색 {r['scans']}회",
                f"5분 비상승률: {rate}"+(' · 표본 부족(<30건)' if r['evaluated']<30 else ''),
                f"평가 대기 {r['pending']} · 평가 불가 {r['unavailable']}",''])
    lines.append('관측 중단·평가 미수집은 비상승 분모에서 제외합니다. 버전별 기간과 감시 범위가 달라 동시 A/B 검증이 아닙니다.')
    return '\n'.join(lines)
