"""Telegram read-only MAGI3 views; private service token never enters messages."""
import os
import time
import requests
from magi3.accounts import render_accounts,money


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
    lines=['🧪 Shadow 모의 실행 [실자산 아님]',f'갱신 {elapsed}초 전'+(' · 오래된 자료' if elapsed>60 else ''),
           '가상 현금: '+money(r['cash_krw']),'총 평가: '+money(r['equity_krw']),
           '실현손익: '+money(r['realized_pnl_krw']),'평가손익: '+money(r['unrealized_pnl_krw']),
           f"수수료 가정: 편도 {r['fee_bps_per_side']:g}bps · 누적 {money(r['fees_krw'])}",'']
    for p in r['positions']:
        lines.append(f"{p['venue'].upper()} {p['asset']} {p['qty']:.8g} | {money(p['value_krw'])} | PICK={'+'.join(p['strategy_tags'])}")
    if not r['positions']:lines.append('보유 없음')
    for tag,summary in r.get('strategy_summary',{}).items():
        lines.append(f"{tag} | 실현 {money(summary['realized_pnl_krw'])} · 평가 {money(summary['unrealized_pnl_krw'])}")
    lines+=['','공개 호가 기반 모의 IOC 체결·미체결 잔량 취소. 평가손익에는 향후 매도 수수료가 빠집니다.',
            'VPD≥75 저장본당 1회 · 1만원 · 300초 후 모의 청산. 수익성 검증 완료 전략이 아닙니다.',
            '주문 이력 /orders · 실제 계좌 /assets']
    return '\n'.join(lines)


def render_orders(r):
    lines=['📒 Shadow 주문·체결 원장 [실주문 아님]']
    for o in r['orders']:
        qty='-' if o['qty'] is None else f"{o['qty']:.8g}"
        lines.append(f"{o['side']} {o['venue'].upper()} {o['asset']} {qty} | {o['status']}\n체결 {money(o['notional'])} · 수수료 {money(o['fee'])} · 실현 {money(o['realized'])}\nID {o['id'][:12]}")
    if not r['orders']:lines.append('아직 모의 체결이 없습니다.')
    return '\n\n'.join(lines)


def render_status(r):
    elapsed=age(r,'heartbeat_ms') if r.get('heartbeat_ms') else None
    heartbeat='시작 중' if elapsed is None else f'{elapsed}초 전'+(' · 응답 지연' if elapsed>60 else '')
    return (f"🧩 MAGI3 운영 상태\n모드: {r['mode']} · 실주문 OFF\n최근 동작: {heartbeat}\n"
            f"실행 루프: {r['loop_status']}\nMAGI2 신호: {r['feed_status']}\n"
            f"청산 대기: {r.get('pending_exit_count',0)}건\n영속 원장: 연결됨\n\n"
            'Shadow /shadow · 주문원장 /orders\n실계좌 조회 /assets · PAPER /report')


def view(command):
    path,renderer={'assets':('/accounts',render_accounts),'shadow':('/shadow',render_shadow),
                   'orders':('/orders',render_orders),'magi3':('/status',render_status),
                   'execution':('/status',render_status)}[command]
    try:return renderer(fetch(path))
    except Exception:return 'MAGI3 조회 서비스에 연결하지 못했거나 첫 자료를 준비 중입니다. 잠시 후 다시 조회하세요. 실제 잔고나 실행 상태가 정상이라는 뜻은 아닙니다.'
