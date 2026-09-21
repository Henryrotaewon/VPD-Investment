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
    from magi2.fast_session import day, bounds
    from magi2.fast_paper_report import keyboard, NAMES, clock
    date=day(ts); start,_=bounds(date)
    with audit.lock:
        rows=audit.db.execute("SELECT ts_ms,venue,symbol,payload FROM fast_events "
            "WHERE event_type='SIGNAL_DETECTED' AND ts_ms>=? AND ts_ms<=? "
            "ORDER BY ts_ms DESC,signal_id DESC",(start,ts)).fetchall()
    latest={}
    for stamp,venue,symbol,payload in rows:
        event=json.loads(payload)
        if event.get('rule_version')!='fast-price-rise-v4': continue
        latest.setdefault((venue,symbol),(stamp,venue,event.get('observed',{}).get('asset') or symbol))
    selected=list(latest.values()); size=15
    offset=min(max(0,int(offset))//size*size,max(0,(len(selected)-1)//size*size))
    lines=['⚡ FAST 포착 리스트',f'{date} 07:30 이후 · {len(selected)}종목 · 최신순',
           f'{offset//size+1}/{max(1,(len(selected)+size-1)//size)}페이지','']
    lines += [f'{NAMES.get(venue,venue)} · {asset} · {clock(stamp)}' for stamp,venue,asset in selected[offset:offset+size]]
    if not selected: lines.append('당일 포착 종목이 없습니다.')
    markup=keyboard();nav=[]
    if offset:nav.append({'text':'◀ 이전','callback_data':f'captures:{offset-size}'})
    nav.append({'text':'새로고침','callback_data':f'captures:{offset}'})
    if offset+size<len(selected):nav.append({'text':'다음 ▶','callback_data':f'captures:{offset+size}'})
    markup['inline_keyboard'].insert(0,nav)
    return '\n'.join(lines),markup
