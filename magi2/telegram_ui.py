"""Korean Telegram navigation and actor-bound, one-use PAPER confirmations."""
import secrets
import time
from magi3.accounts import quantity

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
    ('shadows', 'shadows 모의투자 메뉴'), ('shadow', 'Shadow 현재 자산현황'), ('orders', 'Shadow 최근 3일 매매이력'),
    ('scan', '최근 VPD 조회 · 오전/저녁 선택'),
    ('morning_scan', '오전 VPD 저장본 조회'), ('evening_scan', '저녁 VPD 저장본 조회'),
    ('signals', 'FAST 포착 조회 · 기존 명령'),
    ('fast', 'FAST 포착 · 최근 24시간'), ('fast_compare', 'FAST 거래소별 신호·오탐 비교'),
    ('wave', 'WAVE 강도·전파·매매 검증'), ('strategies', '전략 검증 기준'),
    ('morning', 'PAPER 리밸런싱 · 확인 후 실행'),
    ('rebuild', 'PAPER 전량 교체 · 최신 VPD로 재구성'),
    ('refill', 'PAPER 빈자리 매수 · 확인 후 실행'), ('cancel', '대기 중 실행 확인 취소'),
]
LABELS = {
    '📊 VPD 모의투자': 'vpd', '💼 실계좌 자산': 'assets',
    '🧪 shadows 모의투자': 'shadows', '⚡ FAST 포착': 'fast',
    '🧭 전략검증': 'strategies', '🤖 시스템 상태': 'status',
    '🧩 MAGI 역할': 'about',
}
ALIASES = {
    '⚡ fast 후보': 'fast', '⚡ fast 포착': 'fast', 'fast 포착': 'fast',
    '🧪 shadow 자산': 'shadow', '📒 shadow 원장': 'orders', 'shadows 모의투자': 'shadows',
    '🐋 whale 참고': 'wave',
    '🔎 vpd 조회': 'scan', '❓ 도움말': 'help', '📋 메뉴': 'menu',
    '🌐 WAVE 근거': 'wave', '⚡ 신호조회': 'signals',
    '🔄 paper 리밸런싱': 'morning', '♻️ paper 빈자리 채우기': 'refill',
    '리밸런싱': 'morning', '리벨런싱': 'morning', '종목리필': 'refill', '종목 리필': 'refill',
    '전량 교체': 'rebuild', '전량교체': 'rebuild', '🔁 전량 교체': 'rebuild', '전체 리밸런싱': 'rebuild',
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
    return text if text in dict(COMMANDS) or text in ('execution','magi3','status1','status2','status3','wave') else None


def main_keyboard():
    rows = [list(LABELS)[n:n+2] for n in range(0, len(LABELS), 2)]
    return {'keyboard': [[{'text': x} for x in row] for row in rows],
            'resize_keyboard': True, 'is_persistent': True,
            'input_field_placeholder': 'MAGI · 메뉴를 선택하거나 /help를 입력하세요'}


def role_text():
    return ('🧩 MAGI — 시장 관측 · 전략 검증 · 실행 관리\n\n'
            'MAGI1 · 시장 관측\n시세·체결·호가와 FAST 후보, WAVE 근거를 관측합니다.\n'
            'WHALE은 독립 조회 없이 WAVE 보조지표의 추가 효과를 검증하는 데이터로 보존합니다.\n\n'
            'MAGI2 · 전략 검증\nVPD 분석과 PAPER 모의투자를 수행합니다. '
            'FAST 재생 평가는 연구 도구 단계입니다.\n\n'
            'MAGI3 · 자산·실행 관리\n실계좌 잔고와 Shadow 모의 체결을 구분합니다. '
            '실거래 활성화 여부는 시스템 상태에서 확인하세요.\n\n'
            '아래 조회 메뉴는 매매를 시작하지 않습니다.')


def role_keyboard():
    groups = [
        [('MAGI2 · VPD 조회','scan'), ('MAGI2 · VPD 모의투자','vpd')],
        [('MAGI3 · 실계좌 자산','assets'), ('MAGI3 · shadows 모의투자','shadows')],
        [('MAGI1 상태','status1'), ('MAGI2 상태','status2'), ('MAGI3 상태','status3')],
    ]
    return {'inline_keyboard': [[{'text': label, 'callback_data': 'nav:'+cmd}
                                for label,cmd in row] for row in groups]}


def shadow_keyboard():
    return {'inline_keyboard': [
        [{'text': '📊 자산현황(현재)', 'callback_data': 'nav:shadow'},
         {'text': '📒 최근 3일 매매이력', 'callback_data': 'nav:orders'}],
        [{'text': '↩️ 메인 메뉴', 'callback_data': 'nav:menu'}]]}


def vpd_keyboard():
    return {'inline_keyboard': [
        [{'text': '📊 현황 보고', 'callback_data': 'nav:report'},
         {'text': '🔎 VPD 조회', 'callback_data': 'nav:scan'}],
        [{'text': '🔄 리밸런싱', 'callback_data': 'nav:morning'},
         {'text': '♻️ 종목 리필', 'callback_data': 'nav:refill'}],
        [{'text': '🔁 전량 교체', 'callback_data': 'nav:rebuild'}],
        [{'text': '↩️ 메인 메뉴', 'callback_data': 'nav:menu'}],
    ]}


def status_keyboard():
    return {'inline_keyboard': [[{'text': label, 'callback_data': 'nav:'+command}]
        for label, command in [('MAGI1 · 시세·관측', 'status1'),
                               ('MAGI2 • 전략 • 검증', 'status2'),
                               ('MAGI3 · 계좌·실행', 'status3')]]}


def scan_keyboard():
    return {'inline_keyboard': [[{'text': '🌅 오전 VPD', 'callback_data': 'nav:morning_scan'},
                                 {'text': '🌙 저녁 VPD', 'callback_data': 'nav:evening_scan'}],
        [{'text': '↩️ VPD 모의투자', 'callback_data': 'nav:vpd'}]]}


def help_text():
    return ('🤖 MAGI 도움말\n시장 관측 → 전략 검증 → 자산·실행 관리\n/about — MAGI1·2·3 소개와 역할별 메뉴\n\n[조회 · 거래 없음]\n'
            '/vpd — VPD 모의투자 메뉴 (현황·VPD 조회·리밸런싱·종목 리필·전량 교체)\n/report — VPD 모의투자 현황 (가상자금)\n/assets — 실계좌 자산 (거래소 실제 잔고)\n'
            '/scan — 오전·저녁 VPD 선택\n/morning_scan · /evening_scan — 저장본 조회\n'
            '/signals · /fast — FAST 포착 · 최근 24시간\n/fast_compare — 거래소별 신호·오탐 비교\n/wave — WAVE 신호 강도·전파 근거·매매 가능성\n/strategies — 전략 검증 기준\n'
            '/shadows — shadows 모의투자 메뉴\n/shadow — 현재 자산현황\n/orders — 최근 3일 매매이력\n'
            '/status — MAGI1·2·3 상태 선택\n/execution · /magi3 — 기존 MAGI3 상태 명령도 지원\n\n'
            '[PAPER 실행 · 확인 버튼 필요]\n/morning — 보유 판단 후 리밸런싱\n/rebuild — 전량 매도 후 새 VPD TOP10 균등 매수 (보유·당일 재진입 유예 해제)\n/refill — 빈자리 채우기\n'
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
        lines+=['','🐋 WHALE 참고 — 대규모 온체인 거래',
                'WHALE은 WAVE의 여러 입력 중 하나이며 독립 매매 전략이 아닙니다.',
                '거래소 간 WAVE 시작·확산 분석은 이 화면에 아직 연결되지 않았습니다.',
                f'{len(whale)}건 중 최근 {min(15,len(whale))}건']
        for r in whale[-15:]:
            e=r.get('evidence',{});amount=e.get('amount')
            amount_text=f"{quantity(amount)} {r['asset']}" if isinstance(amount,(int,float)) else '전송량 미확인'
            raw=e.get('classification')=='unclassified_public_raw' or (e.get('transaction') or {}).get('raw_provider')=='blockchain-info-public-ws'
            amount_label='거래 출력 합계' if raw else '관측 수량'
            status=(e.get('transaction') or {}).get('confirmation_status')
            confirmation='미확정 거래' if status=='UNCONFIRMED' else '확정 상태 미확인'
            lines.append(f"{clock(r)} · {amount_label} {amount_text} · {confirmation}")
        if not whale:lines.append('이 구간의 대규모 전송 관측 없음')
        lines+=['지갑 소유자·거래소 입출금 여부·매수/매도 방향은 확인되지 않았습니다. 거래 출력 합계에는 거스름돈·내부 이동이 포함될 수 있습니다. 고래 순이동량이나 매매량을 뜻하지 않습니다.',
                '이 화면은 온체인 참고자료입니다. 거래소 간 전파·선행성 분석의 전체 결과가 아닙니다.']
    lines+=['','현재는 후보·근거 조회이며 수익성 검증 성적표가 아닙니다. /strategies']
    return '\n'.join(lines)


def magi3_status():
    from magi2.execution_client import view
    return view('magi3')


def validation_text():
    return ('🧭 전략검증\n전략별 설명·매매 공략과 검증 진행 상황을 확인하세요. WAVE는 실제 관측 및 시간 이동 대조군 분석으로 연결됩니다.\n\n'
            '🌐 WAVE: 거래소 간 움직임의 시작과 확산\n'
            '📊 VPD: 상위 후보 분산 모의투자\n'
            '⚡ FAST: 단기 순위 급등 포착·짧은 반복 매매\n\n'
            '이 메뉴는 분석·설명 조회입니다. 버튼을 눌러도 매매를 시작하지 않습니다. '
            '연구 가설과 현재 운영 규칙을 구분해 표시하며 검증된 수익을 보장하지 않습니다.')


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
        label = '🔴 전량 매도 후 재매수' if action=='rebuild' else '✅ PAPER 실행'
        return {'inline_keyboard': [[{'text': label, 'callback_data': 'confirm:'+token},
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
