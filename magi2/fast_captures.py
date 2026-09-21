"""High-buy-flow FAST display and durable notification limits, not probability claims."""
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import math

FILTER_VERSION='fast-buy-flow-v1'

def flow_metrics(rows,ts):
    """Rows=(exchange_ms, price, quantity, buyer_taker). Bounded recent sample."""
    valid=[]
    for t,p,q,buy in rows:
        if 0<=ts-t<=30000 and all(math.isfinite(v) and v>0 for v in (p,q)) and isinstance(buy,bool):
            valid.append((t,p*q,buy))
    total=sum(x[1] for x in valid);buy=sum(x[1] for x in valid if x[2])
    span=max(x[0] for x in valid)-min(x[0] for x in valid) if valid else 0
    newest=max(x[0] for x in valid) if valid else None
    sufficient=len(valid)>=20 and span>=5000 and newest is not None and ts-newest<=3000
    return {'sample_trades':len(valid),'sample_span_ms':span,'latest_trade_age_ms':ts-newest if newest is not None else None,
            'sample_quote_value':total,'buyer_quote_value':buy,'buyer_share_pct':buy/total*100 if total else None,
            'sufficient':sufficient,'window_limit_ms':30000,'basis':'RECENT_BOUNDED_TAKER_SAMPLE'}

def strength(flow,metrics):
    share=flow.get('buyer_share_pct')
    strong=bool(flow.get('sufficient') and share is not None and share>=70 and metrics['spread_bps']<=10 and metrics['breakout_bps']>=10)
    return {'version':FILTER_VERSION,'strong':strong,'score':round(share,1) if flow.get('sufficient') and share is not None else None,
            'label':'강함' if strong else '약함/조건 미달','flow':flow,
            'thresholds':{'min_buyer_share_pct':70,'min_sample_trades':20,'min_span_ms':5000,'max_trade_age_ms':3000,'max_spread_bps':10,'min_breakout_bps':10}}

class AlertGate:
    def __init__(self,audit):self.audit=audit
    def claim(self,signal,venue,symbol,asset,ts):
        asset={'XBT':'BTC','XXBT':'BTC','XETH':'ETH'}.get(asset,asset)
        with self.audit.lock:
            rows=self.audit.db.execute("SELECT ts_ms,payload FROM fast_events WHERE event_type='ALERT_RESERVED' AND ts_ms>? ORDER BY ts_ms",(ts-3600000,)).fetchall()
            if any(json.loads(r[1]).get('observed',{}).get('asset')==asset for r in rows):return 'SAME_ASSET_1H'
            if len(rows)>=3:return 'GLOBAL_3_PER_HOUR'
            if rows and ts-rows[-1][0]<600000:return 'GLOBAL_10_MINUTE_GAP'
            self.audit.record('ALERT_RESERVED',signal,venue,symbol,'ALERT_ONLY',ts_ms=ts,
                              rule_version=FILTER_VERSION,observed={'asset':asset})
            return None

def captures(audit,ts,tracking=None,offset=0):
    tracking=tracking or {};since=ts-86400000
    with audit.lock:
        rows=audit.db.execute("SELECT ts_ms,signal_id,event_type,venue,symbol,payload FROM fast_events WHERE ts_ms>=? AND ts_ms<=? AND event_type IN ('SIGNAL_DETECTED','SIGNAL_EVALUATED','WATCH_ENDED') ORDER BY ts_ms",(ts-7*86400000,ts)).fetchall()
    signals={};outcomes={};ends={}
    for t,ident,kind,venue,symbol,payload in rows:
        e=json.loads(payload)
        if kind=='SIGNAL_DETECTED':signals.setdefault(ident,dict(e,ts_ms=t,venue=venue,symbol=symbol,signal_id=ident))
        elif kind=='SIGNAL_EVALUATED':outcomes.setdefault(ident,(t,e.get('result',{})))
        else:ends[ident]=(t,e.get('observed',{}))
    latest={};all_count=0
    for s in sorted(signals.values(),key=lambda s:s['ts_ms'],reverse=True):
        if s['ts_ms']<since:continue
        all_count+=1
        if s.get('observed',{}).get('strength',{}).get('strong'):
            latest.setdefault((s['venue'],s['symbol']),s)
    selected=list(latest.values());offset=max(0,int(offset));page=selected[offset:offset+10]
    lines=['⚡ FAST 포착 · 최근 24시간',f'조건 충족 종목 {len(selected)}개 · 전체 포착 {all_count}건',
           'v2=5분 거래대금·가격 가속, v1=매수 주도 체결. 같은 거래소·종목은 최신 포착만 표시합니다.',
           '알림은 시간 제한이 있어도 포착·모의투자 판단은 모두 기록합니다.',
           '유지 예측확률: 산출 대기 — 검증된 확률 모델이 없습니다.','']
    for s in page:
        obs=s['observed'];power=obs['strength'];flow=power.get('flow',{});age=max(0,(ts-s['ts_ms'])//60000)
        clock=datetime.fromtimestamp(s['ts_ms']/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M')
        lines.append(f"{s['venue'].upper()} {obs.get('asset') or s['symbol']} · {clock} KST · {age}분 경과")
        if power.get('version')=='fast-volume-accel-v2':
            v=obs['volume_acceleration']
            lines.append(f"v2 · 거래대금 {v['turnover_ratio']:.2f}배 · 직전 5분 {v['previous_return_5m_bps']/100:+.2f}% → 최근 {v['return_5m_bps']/100:+.2f}%")
        else:
            lines.append(f"v1 포착 강도 {power['score']:.1f}/100 · 매수비중 {flow['buyer_share_pct']:.1f}% ({flow['sample_trades']}체결 표본)")
        lines.append(f"5분 상승 {obs['returns_bps']['5']/100:+.2f}% · 스프레드 {obs['spread_bps']/100:.3f}%")
        current=tracking.get(s['signal_id'])
        if not current and s['signal_id'] in ends:
            endts,end=ends[s['signal_id']];current={'bid':(end.get('last_quote') or {}).get('bid'),'ts_ms':end.get('last_quote_ts_ms'),'until':endts}
        if current and current.get('bid') and current.get('ts_ms') and obs.get('ask'):
            elapsed=max(0,(ts-current['ts_ms'])//1000);ret=(current['bid']/obs['ask']-1)*100
            phase='추적 중' if ts<current['until'] and elapsed<=5 else '마지막 관측'
            lines.append(f"경과 {ret:+.2f}% · {phase} {elapsed}초 전 (수수료 미차감)")
        result=outcomes.get(s['signal_id'],(None,{}))[1]
        if result.get('status')=='EVALUATED':lines.append(f"5분 결과: {'상승 유지' if not result['non_rising'] else '비상승'} {result['mark_return_bps']/100:+.2f}%")
        else:lines.append('5분 결과: '+('평가 대기' if ts-s['ts_ms']<=305000 else '평가 불가'))
        # Historical empirical reference only, completed before this detection (no look-ahead).
        history=[]
        for h in signals.values():
            hp=h.get('observed',{}).get('strength',{});out=outcomes.get(h['signal_id'])
            if h['venue']==s['venue'] and h['ts_ms']<s['ts_ms'] and hp.get('version')==power.get('version') and hp.get('strong') and out and out[0]<s['ts_ms'] and out[1].get('status')=='EVALUATED':history.append(not out[1]['non_rising'])
        if len(history)>=30:lines.append(f"과거 강한 신호 5분 유지율 {sum(history)/len(history)*100:.1f}% · n={len(history)} (예측확률 아님)")
        else:lines.append(f'유지율 참고 표본 {len(history)}/30건 · 집계 대기')
        lines.append('')
    if not page:lines.append('현재 선정 조건을 충족한 포착 기록이 없습니다.')
    markup={'inline_keyboard':[]}
    if offset:markup['inline_keyboard'].append([{'text':'◀ 이전','callback_data':f'captures:{max(0,offset-10)}'}])
    if offset+10<len(selected):markup['inline_keyboard'].append([{'text':'다음 ▶','callback_data':f'captures:{offset+10}'}])
    markup['inline_keyboard'].append([{'text':'📊 FAST 모의검증 결과','callback_data':'nav:fast_report'}])
    markup['inline_keyboard'] += [[{'text':'새로고침','callback_data':'nav:fast'},{'text':'거래소별 검증','callback_data':'nav:fast_compare'}],[{'text':'메인 메뉴','callback_data':'nav:menu'}]]
    return '\n'.join(lines),markup
