"""Korean Telegram navigation and actor-bound, one-use PAPER confirmations."""
import secrets
import time

COMMANDS = [
    ('help', '도움말 · 전체 명령어'), ('menu', '버튼 메뉴 열기'),
    ('status', '봇 상태 · PAPER 작업 상태'), ('report', 'PAPER 자산보고'),
    ('assets', '4개 거래소 실계좌 통합자산 · 조회 전용'),
    ('shadow', 'Shadow 모의 자산·손익'), ('orders', 'Shadow 주문·체결 원장'),
    ('execution', 'MAGI3 운영 상태'),
    ('scan', '최근 VPD 조회 · 오전/저녁 선택'),
    ('morning_scan', '오전 VPD 저장본 조회'), ('evening_scan', '저녁 VPD 저장본 조회'),
    ('signals', 'FAST · Whale 관측 신호'), ('strategies', '전략 검증 기준'),
    ('magi3', 'MAGI3 운영 상태 · execution과 동일'), ('morning', 'PAPER 리밸런싱 · 확인 후 실행'),
    ('refill', 'PAPER 빈자리 매수 · 확인 후 실행'), ('cancel', '대기 중 실행 확인 취소'),
]
LABELS = {
    '📊 자산보고': 'report', '💼 통합자산': 'assets', '🔎 VPD 조회': 'scan',
    '🧪 Shadow 자산': 'shadow', '📒 Shadow 원장': 'orders',
    '⚙️ 실행 상태': 'execution', '⚡ 신호조회': 'signals', '🧭 전략검증': 'strategies', '🤖 상태': 'status',
    '🔄 PAPER 리밸런싱': 'morning', '♻️ PAPER 빈자리 채우기': 'refill',
    '❓ 도움말': 'help', '📋 메뉴': 'menu',
}
ALIASES = {
    'start': 'menu', '도움말': 'help', '메뉴': 'menu', '상태': 'status',
    '보고서': 'report', '자산보고': 'report', '통합자산': 'assets',
    '모의자산': 'shadow', '주문원장': 'orders', '실행상태': 'execution',
    '신호': 'signals', '전략': 'strategies', '취소': 'cancel',
    'morning scan': 'morning_scan', 'evening scan': 'evening_scan',
    'magi1 morning scan': 'morning_scan', 'magi1 evening scan': 'evening_scan',
    'magi2 morning': 'morning', 'magi2 refill': 'refill', 'magi2 report': 'report',
    'magi2 help': 'help', 'magi3 report': 'assets', 'magi3 status': 'magi3',
}


def parse_command(text, bot_username=''):
    text = ' '.join(text.strip().split())
    if text in LABELS:
        return LABELS[text]
    if text.startswith('/'):
        head, *tail = text[1:].split(' ', 1)
        if '@' in head:
            head, target = head.split('@', 1)
            if not bot_username or target.lower() != bot_username.lower():
                return None
        text = ' '.join([head] + tail)
    text = text.lower()
    text = ALIASES.get(text, text)
    return text if text in dict(COMMANDS) else None


def main_keyboard():
    rows = [list(LABELS)[n:n+2] for n in range(0, len(LABELS), 2)]
    return {'keyboard': [[{'text': x} for x in row] for row in rows],
            'resize_keyboard': True, 'is_persistent': True,
            'input_field_placeholder': '버튼을 누르거나 /help를 입력하세요'}


def scan_keyboard():
    return {'inline_keyboard': [[{'text': '🌅 오전 VPD', 'callback_data': 'nav:morning_scan'},
                                 {'text': '🌙 저녁 VPD', 'callback_data': 'nav:evening_scan'}]]}


def help_text():
    return ('🤖 명령어 안내\n\n[조회 · 거래 없음]\n'
            '/report — PAPER 자산·보유종목\n/assets — 4개 거래소 실계좌 통합자산\n'
            '/scan — 오전·저녁 VPD 선택\n/morning_scan · /evening_scan — 저장본 조회\n'
            '/signals — FAST · Whale 신호\n/strategies — 전략 검증 기준\n'
            '/shadow — Shadow 모의 자산·손익\n/orders — Shadow 주문·체결 원장\n'
            '/status — 봇·작업 상태\n/execution · /magi3 — MAGI3 운영 상태\n\n'
            '[PAPER 실행 · 확인 버튼 필요]\n/morning — 리밸런싱\n/refill — 빈자리 채우기\n'
            '/cancel — 대기 중 확인 취소 (진행 중 작업 중단 아님)\n\n'
            '[화면]\n/menu — 버튼 메뉴\n/help — 이 안내\n\n'
            'magi 접두어 없이 report, help 또는 한글 버튼을 사용하세요. 기존 명령도 지원합니다.\n'
            'VPD 조회는 신규 스캔을 실행하지 않습니다. 실거래 시작 명령은 제공하지 않습니다.')


def observation_text(rows):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ordered=sorted(rows,key=lambda r:(r['event_ts_ms'],r.get('observation_id','')))
    lines=['⚡ FAST · Whale 관측 [매매 지시 아님]',
           f'최근 5분 {len(rows)}건 중 최근 {min(15,len(rows))}건 · 시각 KST',
           '점수는 관측 강도이며 성공확률이 아닙니다.','']
    directions={'BUY':'상승 쪽 가속','SELL':'하락 쪽 가속'}
    for row in ordered[-15:]:
        clock=datetime.fromtimestamp(row['event_ts_ms']/1000,ZoneInfo('Asia/Seoul')).strftime('%H:%M:%S')
        evidence=row.get('evidence',{});score=row['heuristic_score']
        if row['strategy_tag']=='FAST':
            label=directions.get(row['direction'],'방향 미확인')
            lines.append(f"{clock} {row['asset']} · FAST · {evidence.get('venue','미확인')}\n{row['direction']} ({label}) · 강도 {score:.1f}")
        else:
            amount=evidence.get('amount')
            amount_text=f" · 전송 {amount:,.2f} {row['asset']}" if isinstance(amount,(int,float)) else ''
            lines.append(f"{clock} {row['asset']} · WHALE · onchain\n{row['direction']} (입출금 방향 미확인) · 규모점수 {score:.1f}{amount_text}")
    if not rows:lines.append('이 관측 구간에 신호가 없습니다.')
    lines+=['','FAST BUY는 하락 둔화도 포함합니다. WHALE 전송에는 거스름돈·내부 이동이 포함될 수 있습니다.',
            '관측 목록이며 검증 성적표가 아닙니다. 기준은 /strategies에서 확인하세요.']
    return '\n'.join(lines)


def magi3_status():
    from magi2.execution_client import view
    return view('magi3')


def validation_text():
    return ('🧭 FAST · Whale 검증 기준\n\n'
            '1. 신호 발생 뒤의 10·30·60·300초를 평가합니다.\n'
            '2. FAST는 신호가 나온 거래소별로 분리합니다. Whale은 주소 소유·입출금 방향을 '
            '입증하지 못하면 UNKNOWN 관측으로 유지합니다.\n'
            '3. 매수는 ask 진입 → bid 청산에 왕복 수수료·슬리피지 가정을 더합니다. '
            'SELL 신호를 현물 공매도 수익으로 계산하지 않습니다.\n'
            '4. 데이터 누락·호가 공백은 실패 거래와 구분하고 제외율을 함께 봅니다.\n'
            '5. 같은 종목·거래소·시간대의 비신호 구간 및 VPD 단독과 비교합니다. '
            '중첩 이벤트를 묶고 날짜 순 학습/검증 구간을 분리합니다.\n'
            '6. 순수익·적중률·최대 역행·표본수·신뢰구간을 확인합니다. '
            '점수는 검증된 성공확률이 아닙니다.\n\n'
            '현재 수익성 판정: 미검증. 검증 통과 전 실거래 신호로 승격하지 않습니다.')


class Confirmations:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = {}

    def issue(self, action, chat_id, user_id):
        self.cancel(chat_id, user_id)
        now = self.clock()
        self.pending = {k:v for k,v in self.pending.items() if now < v[3]}
        token = secrets.token_hex(8)
        self.pending[token] = (action, str(chat_id), str(user_id), now+60)
        return {'inline_keyboard': [[{'text': '✅ PAPER 실행', 'callback_data': 'confirm:'+token},
                                    {'text': '취소', 'callback_data': 'cancel:'+token}]]}

    def consume(self, token, chat_id, user_id):
        value = self.pending.get(token)
        if not value or value[1:3] != (str(chat_id),str(user_id)):
            return None
        self.pending.pop(token, None)
        return value[0] if self.clock() < value[3] else None

    def cancel(self, chat_id, user_id):
        self.pending = {k:v for k,v in self.pending.items()
                        if v[1:3] != (str(chat_id),str(user_id))}
