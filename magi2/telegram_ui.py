"""Korean Telegram navigation and actor-bound, one-use PAPER confirmations."""
import secrets
import time

BOT_NAME = 'MAGI'
BOT_SHORT_DESCRIPTION = 'MAGI | 코인 시장 관측·전략 검증·자산 관리. VPD · FAST · WAVE'
BOT_DESCRIPTION = ('MAGI — 코인 시장 분석과 투자 현황을 한곳에서.\n'
    'MAGI1: 시장 데이터·FAST·WAVE 관측\n'
    'MAGI2: VPD 분석·모의투자·전략 검증\n'
    'MAGI3: 실계좌 조회·Shadow 검증·실행 관리\n'
    '모의투자와 실제 자산을 구분해 확인하세요. /menu로 시작합니다.')

COMMANDS = [
    ('help', 'MAGI 도움말 · 전체 명령어'), ('about', 'MAGI 소개 · 역할별 메뉴'), ('menu', '버튼 메뉴 열기'),
    ('status', 'MAGI1·2·3 상태 선택'), ('vpd', 'VPD 모의투자 메뉴'), ('report', 'VPD 모의투자 현황'),
    ('assets', '실계좌 자산 · 거래소별 조회'),
    ('shadow', 'Shadow 모의 자산·손익'), ('orders', 'Shadow 주문·체결 원장'),
    ('scan', '최근 VPD 조회 · 오전/저녁 선택'),
    ('morning_scan', '오전 VPD 저장본 조회'), ('evening_scan', '저녁 VPD 저장본 조회'),
    ('signals', 'FAST 후보 · WAVE 기초자료 요약'),
    ('fast', '거래소별 급등 후보'), ('wave', '글로벌 WAVE 검토용 온체인 근거'), ('strategies', '전략 검증 기준'),
    ('morning', 'PAPER 리밸런싱 · 확인 후 실행'),
    ('refill', 'PAPER 빈자리 매수 · 확인 후 실행'), ('cancel', '대기 중 실행 확인 취소'),
]
LABELS = {
    '📊 VPD 모의투자': 'vpd', '💼 실계좌 자산': 'assets', '🔎 VPD 조회': 'scan',
    '🧪 Shadow 자산': 'shadow', '📒 Shadow 원장': 'orders',
    '⚡ FAST 후보': 'fast', '🌐 WAVE 근거': 'wave', '⚡ 신호조회': 'signals', '🧭 전략검증': 'strategies', '🤖 시스템 상태': 'status',
    '🧩 MAGI 역할': 'about', '❓ 도움말': 'help', '📋 메뉴': 'menu',
}
ALIASES = {
    '🔄 paper 리밸런싱': 'morning', '♻️ paper 빈자리 채우기': 'refill',
    '리밸런싱': 'morning', '리벨런싱': 'morning', '종목리필': 'refill', '종목 리필': 'refill',
    '📊 자산보고': 'report', '💼 통합자산': 'assets', '🤖 상태': 'status',
    '⚙️ 실행 상태': 'status', '실행상태': 'status',
    'VPD 모의투자': 'vpd', 'vpd 모의투자': 'vpd', '실계좌 자산': 'assets',
    '소개': 'about', '역할': 'about', 'magi': 'about', 'start': 'menu', '도움말': 'help', '메뉴': 'menu', '상태': 'status',
    '보고서': 'report', '자산보고': 'report', '통합자산': 'assets',
    '모의자산': 'shadow', '주문원장': 'orders', '실행상태': 'status',
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
    return text if text in dict(COMMANDS) or text in ('execution','magi3','status1','status2','status3') else None


def main_keyboard():
    rows = [list(LABELS)[n:n+2] for n in range(0, len(LABELS), 2)]
    return {'keyboard': [[{'text': x} for x in row] for row in rows],
            'resize_keyboard': True, 'is_persistent': True,
            'input_field_placeholder': 'MAGI · 메뉴를 선택하거나 /help를 입력하세요'}


def role_text():
    return ('🧩 MAGI — 시장 관측 · 전략 검증 · 실행 관리\n\n'
            'MAGI1 · 시장 관측\n시세·체결·호가와 FAST 후보, WAVE 근거를 관측합니다.\n'
            'WHALE은 WAVE의 기초자료 중 하나입니다.\n\n'
            'MAGI2 · 전략 검증\nVPD 분석과 PAPER 모의투자를 수행합니다. '
            'FAST 재생 평가는 연구 도구 단계입니다.\n\n'
            'MAGI3 · 자산·실행 관리\n실계좌 잔고와 Shadow 모의 체결을 구분합니다. '
            '실거래 활성화 여부는 시스템 상태에서 확인하세요.\n\n'
            '아래 조회 메뉴는 매매를 시작하지 않습니다.')


def role_keyboard():
    groups = [
        [('MAGI1 · FAST 후보','fast'), ('MAGI1 · WAVE 근거','wave')],
        [('MAGI2 · VPD 조회','scan'), ('MAGI2 · VPD 모의투자','vpd')],
        [('MAGI3 · 실계좌 자산','assets'), ('MAGI3 · Shadow','shadow')],
        [('MAGI1 상태','status1'), ('MAGI2 상태','status2'), ('MAGI3 상태','status3')],
    ]
    return {'inline_keyboard': [[{'text': label, 'callback_data': 'nav:'+cmd}
                                for label,cmd in row] for row in groups]}


def vpd_keyboard():
    return {'inline_keyboard': [
        [{'text': '📊 현황 보고', 'callback_data': 'nav:report'}],
        [{'text': '🔄 리밸런싱', 'callback_data': 'nav:morning'},
         {'text': '♻️ 종목 리필', 'callback_data': 'nav:refill'}],
        [{'text': '↩️ 메인 메뉴', 'callback_data': 'nav:menu'}],
    ]}


def status_keyboard():
    return {'inline_keyboard': [[{'text': label, 'callback_data': 'nav:'+command}]
        for label, command in [('MAGI1 · 시세·관측', 'status1'),
                               ('MAGI2 · VPD 모의투자', 'status2'),
                               ('MAGI3 · 계좌·실행', 'status3')]]}


def scan_keyboard():
    return {'inline_keyboard': [[{'text': '🌅 오전 VPD', 'callback_data': 'nav:morning_scan'},
                                 {'text': '🌙 저녁 VPD', 'callback_data': 'nav:evening_scan'}]]}


def help_text():
    return ('🤖 MAGI 도움말\n시장 관측 → 전략 검증 → 자산·실행 관리\n/about — MAGI1·2·3 소개와 역할별 메뉴\n\n[조회 · 거래 없음]\n'
            '/vpd — VPD 모의투자 메뉴 (현황·리밸런싱·종목 리필)\n/report — VPD 모의투자 현황 (가상자금)\n/assets — 실계좌 자산 (거래소 실제 잔고)\n'
            '/scan — 오전·저녁 VPD 선택\n/morning_scan · /evening_scan — 저장본 조회\n'
            '/signals — FAST 후보·WAVE 기초자료 요약\n/fast — 거래소 내 급등 후보\n/wave — WAVE 검토용 온체인 근거\n/strategies — 전략 검증 기준\n'
            '/shadow — Shadow 모의 자산·손익\n/orders — Shadow 주문·체결 원장\n'
            '/status — MAGI1·2·3 상태 선택\n/execution · /magi3 — 기존 MAGI3 상태 명령도 지원\n\n'
            '[PAPER 실행 · 확인 버튼 필요]\n/morning — 리밸런싱\n/refill — 빈자리 채우기\n'
            '/cancel — 대기 중 확인 취소 (진행 중 작업 중단 아님)\n\n'
            '[화면]\n/menu — 버튼 메뉴\n/help — 이 안내\n\n'
            'magi 접두어 없이 report, help 또는 한글 버튼을 사용하세요. 기존 명령도 지원합니다.\n'
            'VPD 조회는 신규 스캔을 실행하지 않습니다. 실거래 시작 명령은 제공하지 않습니다.')


def observation_text(rows,view='signals'):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    def clock(r):return datetime.fromtimestamp(r['event_ts_ms']/1000,ZoneInfo('Asia/Seoul')).strftime('%H:%M:%S')
    ordered=sorted(rows,key=lambda r:(r['event_ts_ms'],r.get('observation_id','')))
    fast=[r for r in ordered if r['strategy_tag']=='FAST' and r.get('evidence',{}).get('fast_rule_version')=='fast-rise-v1' and r['direction']=='BUY']
    whale=[r for r in ordered if r['strategy_tag']=='WHALE']
    legacy=sum(r['strategy_tag']=='FAST' for r in rows)-len(fast)
    lines=['최근 5분 관측 · 시각 KST · 매매 지시 아님']
    if view in ('signals','fast'):
        lines+=['','⚡ FAST — 기존 v1 관측 (새 순위 방식 미연결)',
                '현재 범위: WAVE 공통 선별 종목. 독립 종목 수집·재생 도구는 연구용이며 운영 화면에는 아직 미연결.',
                '초기 연구 기준: 약 10초 상승률 ≥0.20% + 거래량 ≥직전 구간 2배.',
                f'{len(fast)}건 중 최근 {min(15,len(fast))}건']
        for r in fast[-15:]:
            e=r['evidence']
            lines.append(f"{clock(r)} {e.get('venue','-')} · {r['asset']} | 상승 {e['return_bps']/100:+.2f}% | 거래량 {e['volume_ratio']:.2f}배 | 관측 {e['coverage_ms']/1000:.1f}초")
        if not fast:lines.append('현재 기준을 충족한 후보 없음')
        if legacy:lines.append(f'기존 가속 지표 {legacy}건은 새 FAST 후보에서 제외했습니다.')
    if view in ('signals','wave'):
        lines+=['','🌐 글로벌 WAVE 검토 — WHALE 기초자료',
                'WHALE은 WAVE의 여러 입력 중 하나이며 독립 매매 전략이 아닙니다.',
                f'{len(whale)}건 중 최근 {min(15,len(whale))}건']
        for r in whale[-15:]:
            e=r.get('evidence',{});amount=e.get('amount')
            amount_text=f"{amount:,.2f} {r['asset']}" if isinstance(amount,(int,float)) else '전송량 미확인'
            lines.append(f"{clock(r)} · {amount_text} | {r['direction']} | {e.get('classification','unclassified')}")
        if not whale:lines.append('이 구간의 대규모 전송 관측 없음')
        lines+=['주소 소유·거래소 입출금 방향이 확인되지 않으면 UNKNOWN입니다. 거스름돈·내부 이동이 포함될 수 있습니다.',
                '이 화면은 온체인 참고자료입니다. 거래소 간 전파·선행성 분석의 전체 결과가 아닙니다.']
    lines+=['','현재는 후보·근거 조회이며 수익성 검증 성적표가 아닙니다. /strategies']
    return '\n'.join(lines)


def magi3_status():
    from magi2.execution_client import view
    return view('magi3')


def validation_text():
    return ('🧭 전략 역할과 검증 기준\n\n'
            'FAST: 각 거래소 내부의 빠른 상승 종목 포착. 하락 둔화와 SELL은 후보에서 제외합니다.\n'
            'WAVE: 글로벌 거래소 간 움직임의 시작·확산·국내 전파 검토.\n'
            'WHALE: WAVE 검토용 온체인 기초자료 중 하나. 독립 전략이나 매수 지시가 아닙니다.\n\n'
            'FAST 새 개발 방향: 10·15·30분 상승률·순위 급변 탐색 → 상위 소수 후보만 5~10분 관찰 → 짧은 진입·청산 반복. '
            '전 종목 호가 상시 수집은 사용하지 않습니다. 횟수는 목표가 아닌 상한입니다. '
            '순위 계산·반복 PAPER 로직은 구현했고 실시간 수집·화면 연동은 아직 남았습니다. '
            '아래는 기존 v1 평가 기준입니다: 약 10초 +0.20% 이상·거래량 2배 이상. '
            '최적화되거나 수익성이 검증된 기준이 아닙니다. 현재는 WAVE 공통 선별 종목만 감시합니다.\n\n'
            '1. FAST 발생 거래소의 이후 10·30·60·300초를 평가합니다. 주평가는 60초로 고정합니다.\n'
            '2. ask 진입·bid 청산과 왕복 수수료·슬리피지를 반영합니다.\n'
            '3. 겹치는 이벤트를 묶고, 같은 종목·시간대의 비신호 구간과 비교합니다.\n'
            '4. WAVE는 거래소별 수신 지연을 고려합니다. WHALE 근거 추가 전후의 차이를 검토하며 인과관계를 단정하지 않습니다.\n'
            '5. 누락률·독립 표본수·비용 차감 성과·신뢰구간을 미사용 기간에서 확인합니다.\n\n'
            'FAST 비용·지연 반영 오프라인 재생 도구를 구현했습니다. 실데이터 검증과 운영 연동은 남아 있습니다. 현재 수익성 판정: 미검증. '
            'VPD Shadow 실행과 FAST·WAVE 검증은 별개입니다.')


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
