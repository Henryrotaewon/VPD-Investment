"""Telegram read-only MAGI3 views; private service token never enters messages."""
import os
import time
import requests
from magi3.accounts import render_accounts,money,quantity


def fetch(path):
    base=os.getenv('MAGI3_SERVICE_URL','').rstrip('/')
    token=os.getenv('MAGI_SERVICE_TOKEN','')
    if not base or len(token)<32:raise ValueError('SERVICE_NOT_CONFIGURED')
    with requests.Session() as http:
        http.trust_env=False
        response=http.get(base+path,headers={'Authorization':'Bearer '+token},timeout=5)
        response.raise_for_status();return response.json()


def age(report,key='generated_ts_ms'):
    return max(0,(time.time_ns()//1000000-int(report[key]))//1000)


def render_shadow(r):
    elapsed=age(r)
    lines=['🧪 Shadow 자산현황(현재) [실자산 아님]',f'갱신 {elapsed}초 전'+(' · 오래된 자료' if elapsed>60 else ''),
           '가상 현금: '+money(r['cash_krw']),'총 평가: '+money(r['equity_krw']),
           '실현손익: '+money(r['realized_pnl_krw']),'평가손익: '+money(r['unrealized_pnl_krw']),
           f"수수료 가정: 편도 {r['fee_bps_per_side']:g}bps · 누적 {money(r['fees_krw'])}",'']
    for p in r['positions']:
        lines.append(f"{p['venue'].upper()} {p['asset']} {quantity(p['qty'])} | {money(p['value_krw'])} | PICK={'+'.join(p['strategy_tags'])}")
    if not r['positions']:lines.append('보유 없음')
    for tag,summary in r.get('strategy_summary',{}).items():
        lines.append(f"{tag} | 실현 {money(summary['realized_pnl_krw'])} · 평가 {money(summary['unrealized_pnl_krw'])}")
    lines+=['','공개 호가 기반 모의 IOC 체결·미체결 잔량 취소. 평가손익에는 향후 매도 수수료가 빠집니다.',
            'VPD≥75 저장본당 1회 · 1만원 · 300초 후 모의 청산. 수익성 검증 완료 전략이 아닙니다.',
            '주문 이력 /orders · 실제 계좌 /assets']
    return '\n'.join(lines)


def render_orders(r):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    def date(ms):return datetime.fromtimestamp(ms/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')
    def amount(value):return '—' if value is None else money(value)
    lines=['📒 Shadow 최근 3일 매매이력 [실주문 아님]',
           f"{date(r['since_ts_ms'])} ~ {date(r['until_ts_ms'])} KST · 최근 72시간",
           f"전체 {r['total']}건 · {r['offset']+1 if r['orders'] else 0}~{r['offset']+len(r['orders'])}건 표시"]
    for o in r['orders']:
        qty='—' if o['qty'] is None else quantity(o['qty'])
        side={'BUY':'매수','SELL':'매도'}.get(o['side'],o['side'])
        lines.append(f"{date(o['ts_ms'])} · {side} {o['venue'].upper()} {o['asset']} {qty} | {o['status']}\n체결 {amount(o['notional'])} · 수수료 {amount(o['fee'])} · 실현 {amount(o['realized'])}")
    if not r['orders']:lines.append('해당 기간에 모의 매매이력이 없습니다.')
    return '\n\n'.join(lines)


def orders_page(until=None,offset=0):
    from magi2.telegram_ui import shadow_keyboard
    markup=shadow_keyboard()
    try:
        query=f'?offset={int(offset)}'+(f'&until={int(until)}' if until is not None else '')
        r=fetch('/orders/recent'+query)
        text=render_orders(r)
        if r['next_offset'] is not None:
            markup['inline_keyboard'].insert(0,[{'text':'다음 이력 ▶',
                'callback_data':f"orders:{r['until_ts_ms']}:{r['next_offset']}"}])
        return text,markup
    except Exception:
        return '최근 3일 매매이력을 불러오지 못했습니다. 잠시 후 다시 조회하세요.',markup


def render_status(r):
    elapsed=age(r,'heartbeat_ms') if r.get('heartbeat_ms') else None
    heartbeat='시작 중' if elapsed is None else f'{elapsed}초 전'+(' · 응답 지연' if elapsed>60 else '')
    return (f"🧩 MAGI3 운영 상태\n모드: {r['mode']} · 실주문 OFF\n최근 동작: {heartbeat}\n"
            f"실행 루프: {r['loop_status']}\nMAGI2 신호: {r['feed_status']}\n"
            f"청산 대기: {r.get('pending_exit_count',0)}건\n영속 원장: 연결됨\n\n"
            'Shadow /shadow · 주문원장 /orders\n실계좌 조회 /assets · PAPER /report')


def view(command):
    path,renderer={'assets':('/accounts',render_accounts),'shadow':('/shadow',render_shadow),
                   'orders':('/orders/recent',render_orders),'magi3':('/status',render_status),
                   'execution':('/status',render_status)}[command]
    try:return renderer(fetch(path))
    except Exception:return 'MAGI3 조회 서비스에 연결하지 못했거나 첫 자료를 준비 중입니다. 잠시 후 다시 조회하세요. 실제 잔고나 실행 상태가 정상이라는 뜻은 아닙니다.'
