"""Korean Telegram navigation and actor-bound, one-use PAPER confirmations."""
import secrets
import time
from magi3.accounts import quantity

BOT_NAME = 'MAGI'
BOT_SHORT_DESCRIPTION = 'MAGI | 코인 시장 관측·전략 검증·자산 관리. VPD · 지표가속 · FAST 모의투자'
BOT_DESCRIPTION = ('MAGI — 코인 시장 분석과 투자 현황을 한곳에서.\n'
    'MAGI1: 시장 데이터·수급 관측\n'
    'MAGI2: VPD 분석·모의투자·전략 검증\n'
    'MAGI3: 실계좌 조회·Shadow 검증·실행 관리\n'
    '모의투자와 실제 자산을 구분해 확인하세요. /menu로 시작합니다.')

COMMANDS = [
    ('help', 'MAGI 도움말 · 전체 명령어'), ('about', 'MAGI 소개 · 역할별 메뉴'), ('menu', '버튼 메뉴 열기'),
    ('status', 'MAGI1·2·3 상태 선택'), ('vpd', 'VPD 모의투자 메뉴'), ('report', 'VPD 모의투자 현황'),
    ('assets', '실계좌 자산 · 거래소별 조회'),
    ('shadows', 'shadows 모의투자 메뉴'), ('shadow', 'Shadow 현재 자산현황'), ('orders', 'Shadow 최근 3일 매매이력'),
    ('scan', '최근 VPD 조회 · 오전/저녁 선택'), ('rescan', '현재 시점 VPD 재스캔 · 매매 없음'),
    ('morning_scan', '오전 VPD 저장본 조회'), ('evening_scan', '저녁 VPD 저장본 조회'),
    ('indicator', '지표가속 모의투자 메뉴'),
    ('fast_watch', 'FAST 포착·추적 현황 · 주문 없음'),
    ('fast_paper', 'FAST 모의투자 · 300만원 10분할'),
    ('fast_paper_orders', 'FAST 모의 매매기록 · 매수 제외 사유'),
    ('fast_paper_daily', 'FAST 일별 자산평가'),
    ('signals', '지표가속 포착 조회 · 기존 명령'),
    ('fast', '지표가속 메뉴 · 기존 명령 호환'), ('fast_captures', '지표가속 당일 포착 리스트'), ('fast_start', '지표가속 시작 · 현재 설계 검토로 중지'), ('fast_report', '지표가속 모의투자 · 보유 현황·현재 수익률'), ('fast_orders', '지표가속 모의 거래 상세'),
    ('fast_balance', '지표가속 모의투자 · 거래소별 잔고·손익'),
    ('fast_clear', '지표가속 일괄정리 및 포착정지 · 재확인'), ('fast_daily', '지표가속 전일 모의투자 결과'), ('fast_replay', 'FAST 과거 재생검증'), ('fast_compare', '지표가속 관측 상태'),
    ('wave', 'MACD·RSI·거래량·Williams 전략 설명'), ('strategies', '전략 검증 기준'),
    ('regime', '현재 시장 국면 · 투자 참고'),
    ('morning', 'PAPER 리밸런싱 · 확인 후 실행'),
    ('rebuild', 'PAPER 전량 교체 · 최신 VPD로 재구성'),
    ('refill', 'PAPER 빈자리 매수 · 확인 후 실행'), ('cancel', '대기 중 실행 확인 취소'),
]
LABELS = {
    '📊 VPD 모의투자': 'vpd', '💼 실계좌 자산': 'assets',
    '⚡ FAST 포착·추적': 'fast_watch', '🧪 shadows 모의투자': 'shadows', '지표가속 모의투자': 'indicator',
    '⚡ FAST 모의투자': 'fast_paper',
    '🧭 시장 국면': 'regime',
    '🧭 전략검증': 'strategies', '🤖 시스템 상태': 'status',
    '🧩 MAGI 역할': 'about',
}
ALIASES = {
    'fast 관측': 'fast_watch', 'fast 추적': 'fast_watch',
    '지표가속': 'indicator', '지표 가속': 'indicator', '지표가속 모의투자': 'indicator',
    '시장 국면': 'regime', '현재 국면': 'regime', '국면': 'regime', '국면 조회': 'regime',
    '📊 fast 모의검증 결과':'fast_report', '📊 fast 모의결과': 'fast_report', 'fast 모의투자':'fast', 'fast 전일 결과':'fast_daily',
    '📊 fast 모의투자':'fast_report', 'fast 모의검증 결과':'fast_report', 'fast모의검증 결과':'fast_report',
    'fast 모의결과': 'fast_report', 'fast 보고서': 'fast_report', 'fast report': 'fast_report',
    'fast 정리': 'fast_clear', '🧹 fast 정리': 'fast_clear',
    'fast 거래내역': 'fast_orders', 'fast orders': 'fast_orders',
    '⚡ fast 후보': 'fast_captures', '⚡ fast 포착': 'fast_captures', 'fast 포착': 'fast_captures',
    '포착 리스트':'fast_captures', '모의투자 결과':'fast_report',
    '일괄정리 및 포착정지':'fast_clear', '포착 및 매매 시작':'fast_start',
    '🧪 shadow 자산': 'shadow', '📒 shadow 원장': 'orders', 'shadows 모의투자': 'shadows',
    '🐋 whale 참고': 'wave',
    '🔎 vpd 조회': 'scan', '🔄 vpd 재스캔': 'rescan', 'vpd 재스캔': 'rescan', '재스캔': 'rescan', '❓ 도움말': 'help', '📋 메뉴': 'menu',
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
            'MAGI1 · 시장 관측\n시세·체결·호가와 수급 자료를 관측합니다.\n'
            '온체인 관측은 참고 자료이며 새 지표 전략의 매수 신호와 구분합니다.\n\n'
            'MAGI2 · 전략 검증\nVPD 분석과 PAPER 모의투자를 수행합니다. '
            '지표가속은 일봉 회복을 1시간 간격으로 두 번 확인해 진입하고, 추세청산·초기 보호선으로 매도하는 PAPER 전략입니다. '
            'FAST는 단기 수급 신호로 진입하고 보호선·수급 약화로 청산하는 별도 PAPER 후보 전략입니다. 초기 300만원을 최대 10종목에 배분합니다.\n\n'
            'MAGI3 · 자산·실행 관리\n실계좌 잔고와 Shadow 모의 체결을 구분합니다. '
            '실거래 활성화 여부는 시스템 상태에서 확인하세요.\n\n'
            '아래 조회 메뉴는 매매를 시작하지 않습니다.')


def role_keyboard():
    groups = [
        [('MAGI2 · VPD 조회','scan'), ('MAGI2 · VPD 모의투자','vpd')],
        [('MAGI3 · 실계좌 자산','assets'), ('MAGI3 · shadows 모의투자','shadows')],
        [('⚡ FAST 모의투자','fast_paper'), ('⚡ FAST 포착·추적','fast_watch')],
        [('🧭 시장 국면 · 투자 참고','regime')],
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
        [{'text': '🧪 VPD 재스캔 · 매매 없음', 'callback_data': 'nav:rescan'}],
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
            '/scan — 오전·저녁 VPD 선택\n/rescan — 현재 시점 VPD 재스캔 (매매 없음)\n/morning_scan · /evening_scan — 저장본 조회\n'
            '/regime — 현재 시장 국면·사유 (BTC·ETH 완료 일봉, 요청 시 조회)\n'
            '/indicator — 지표가속 모의투자 메뉴\n/fast — 지표가속 메뉴의 기존 명령 호환\n/signals · /fast_captures — 지표가속 포착 이력 (기존 실험 07:30 집계)\n/fast_compare — 지표가속 관측 상태\n/fast_report — 지표가속 총 자산·누적 수익률·보유 종목별 현재 순손익\n/fast_balance — 지표가속 거래소별 잔고·오늘 손익\n/fast_orders — 지표가속 모의 거래 상세\n/fast_clear — 재확인 후 일괄정리 및 포착정지\n/fast_start — 지표가속 시작 (현재 설계 검토로 실행 중지)\n/fast_daily — 지표가속 전일 결과 (매일 07:30 KST 집계)\n/fast_replay — FAST 과거 재생검증\n/wave — 지표가속 전략 설명\n/strategies — 전략 검증 기준\n'
            '지표가속과 FAST는 별도 전략입니다. FAST 모의투자는 /fast_paper에서 확인합니다.\n'
            '/shadows — shadows 모의투자 메뉴\n/shadow — 현재 자산현황\n/orders — 최근 3일 매매이력\n'
            '/status — MAGI1·2·3 상태 선택\n/execution · /magi3 — 기존 MAGI3 상태 명령도 지원\n\n'
            '[PAPER 실행 · 확인 버튼 필요]\n/morning — 보유 판단 후 리밸런싱\n/rebuild — 전량 매도 후 새 VPD TOP10 균등 매수 (보유·당일 재진입 유예 해제)\n/refill — 빈자리 채우기\n'
            '/cancel — 대기 중 확인 취소 (진행 중 작업 중단 아님)\n\n'
            '[화면]\n/menu — 버튼 메뉴\n/help — 이 안내\n\n'
            'magi 접두어 없이 report, help 또는 한글 버튼을 사용하세요. 기존 명령도 지원합니다.\n'
            'VPD 조회는 저장본 조회이며, /rescan만 현재 시점 신규 스캔을 실행합니다. /rescan은 매매하지 않습니다. 실거래 시작 명령은 제공하지 않습니다.')


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
    return ('🧭 전략검증\n\n'
            '⚡ 지표가속: 일봉 회복 1시간 간격 2회 확인 → 진입 · 추세청산/초기 보호선\n'
            'FAST: 단기 수급 확인 진입 · 보호선/수급 약화/시간 청산을 검증하는 300만원·10분할 PAPER 후보 전략\n'
            '📊 VPD: 상위 후보 분산 모의투자\n'
            '지표가속 메뉴에서 업비트 전 종목의 포착·보유 현황·매매 이력·일별 평가를 확인합니다.\n\n'
            '기존 WAVE 전파 전략은 종료했습니다. 신규 전략은 별도 원장으로 검증하며 VPD 성과와 합산하지 않습니다. '
            '설명 조회로 매매가 시작되지는 않습니다. 실주문 없음.')


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
        label = '✅ 확인 · 일괄정리 및 포착정지' if action=='fast_clear' else '✅ 확인 · 포착 및 매매 시작' if action=='fast_start' else '🔴 전량 매도 후 재매수' if action=='rebuild' else '✅ PAPER 실행'
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
