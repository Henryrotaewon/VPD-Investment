"""Korean Telegram navigation and actor-bound, one-use PAPER confirmations."""
import secrets
import time

BOT_NAME = 'MAGI'
BOT_SHORT_DESCRIPTION = 'MAGI | 코인 시장 관측·전략 검증·자산 관리. VPD · 지표가속 · FAST 모의투자'
BOT_DESCRIPTION = ('MAGI — 코인 시장 분석과 투자 현황을 한곳에서.\n'
    'MAGI1: 시장 데이터·수급 관측\n'
    'MAGI2: VPD 분석·모의투자·전략 검증\n'
    'MAGI3: 실계좌 조회·Shadow 검증·실행 관리\n'
    '모의투자와 실제 자산을 구분해 확인하세요. /menu로 시작합니다.')

COMMANDS = [
    ('fast_models','FAST 두 모델 승률·수익률·MDD 비교'),
    ('fast_derivatives','FAST-DERIVATIVES 현선물 모의투자'),
    ('fast_bear','FAST-BEAR 하락장 반등 모의투자'),
    ('fast_model_daily','FAST 두 모델 일별 비교'),
    ('fast_model_data','현물·선물 데이터 수집 점검'),
    ('fast_paper', 'FAST 모의투자 메뉴'),
    ('help', 'MAGI 도움말 · 전체 명령어'), ('about', 'MAGI 소개 · 역할별 메뉴'), ('menu', '버튼 메뉴 열기'),
    ('status', 'MAGI1·2·3 상태 선택'), ('vpd', 'VPD 모의투자 메뉴'), ('report', 'VPD 모의투자 현황'),
    ('assets', '실계좌 자산 · 거래소별 조회'),
    ('shadows', 'shadows 모의투자 메뉴'), ('shadow', 'Shadow 현재 자산현황'), ('orders', 'Shadow 최근 3일 매매이력'),
    ('scan', '최근 VPD 조회 · 오전/저녁 선택'), ('rescan', '현재 시점 VPD 재스캔 · 매매 없음'),
    ('morning_scan', '오전 VPD 저장본 조회'), ('evening_scan', '저녁 VPD 저장본 조회'),
    ('indicator', '지표가속 모의투자 메뉴'),
    ('indicator_rebuild', '지표가속 전량교체 · 유효 후보만 편입'),
    ('indicator_refill', '지표가속 종목 리필 · 가능한 빈자리만 채우기'),
    ('fast_watch', 'FAST 포착·추적 현황 · 주문 없음'),
    ('fast_paper_balance', 'FAST 모의투자 자산현황 · 300만원 10분할'),
    ('fast_paper_orders', 'FAST 모의 매매기록 · 매수 제외 사유'),
    ('fast_paper_daily', 'FAST 일별 자산평가'),
    ('bollinger_orders', '더블볼린저·CCI 매매이력'),
    ('signals', '지표가속 포착 조회 · 기존 명령'),
    ('fast', '지표가속 메뉴 · 기존 명령 호환'), ('fast_captures', '지표가속 당일 포착 리스트'), ('fast_start', '지표가속 시작 · 현재 설계 검토로 중지'), ('fast_report', '지표가속 모의투자 · 보유 현황·현재 수익률'), ('fast_orders', '지표가속 모의 거래 상세'),
    ('fast_balance', '지표가속 모의투자 · 거래소별 잔고·손익'),
    ('fast_clear', '지표가속 일괄정리 및 포착정지 · 재확인'), ('fast_daily', '지표가속 전일 모의투자 결과'), ('fast_replay', 'FAST 과거 재생검증'), ('fast_compare', '지표가속 관측 상태'),
    ('wave', 'MACD·RSI·거래량·Williams 전략 설명'), ('strategies', '네 전략 누적·일별 승률과 수익률'),
    ('regime', '현재 시장 국면 · 투자 참고'),
    ('morning', 'PAPER 리밸런싱 · 확인 후 실행'),
    ('rebuild', 'PAPER 전량 교체 · 최신 VPD로 재구성'),
    ('refill', 'PAPER 빈자리 매수 · 확인 후 실행'), ('cancel', '대기 중 실행 확인 취소'),
]
# Old commands remain accepted, but only the concise navigation is advertised.
LEGACY_COMMANDS = COMMANDS
COMMANDS = [('assets','실투자 현황'), ('paper','모의투자현황'),
            ('regime','시장국면 (MAGI1)'), ('system_info','MAGI 안내·상태')]
MAIN_LABELS = {'실투자 현황':'assets', '모의투자현황':'paper', '시장국면 (MAGI1)':'regime', 'MAGI 안내·상태':'system_info'}
LABELS = {
    **MAIN_LABELS,
    '⚡ FAST 모의투자': 'fast_paper',
    '📊 VPD 모의투자': 'vpd', '💼 실계좌 자산': 'assets',
    '🧪 shadows 모의투자': 'shadows', '지표가속 모의투자': 'indicator',
    '🧭 시장 국면': 'regime',
    '🧭 전략검증': 'strategies', '🤖 시스템 상태': 'status',
    '🧩 MAGI 역할': 'about',
}
ALIASES = {
    '실투자현황':'assets', '모의투자 현황':'paper', '시장국면':'regime',
    '↩️ 메인 메뉴':'menu', '메인 메뉴':'menu',
    'magi 안내·상태':'system_info', 'magi 설명':'system_info', '시스템상태':'system_info',
    '투자결과':'paper_results', '투자전략현황':'paper_status', '투자전략세부':'paper_guide',
    'fast-derivatives':'fast_derivatives', 'fast-bear':'fast_bear',
    '⚡ fast 포착·추적': 'fast_watch',
    'fast모의투자': 'fast_paper', '⚡ fast 모의투자': 'fast_paper',
    'fast 관측': 'fast_watch', 'fast 추적': 'fast_watch',
    '지표가속': 'indicator', '지표 가속': 'indicator', '지표가속 모의투자': 'indicator',
    '지표가속 전량교체':'indicator_rebuild', '지표가속 전량 교체':'indicator_rebuild',
    '지표가속 리필':'indicator_refill', '지표가속 종목 리필':'indicator_refill',
    '시장 국면': 'regime', '현재 국면': 'regime', '국면': 'regime', '국면 조회': 'regime',
    '더블볼린저 매매이력': 'bollinger_orders', '더블볼린저 cci 매매이력': 'bollinger_orders',
    '📊 fast 모의검증 결과':'fast_report', '📊 fast 모의결과': 'fast_report', 'fast 모의투자':'fast_paper', 'fast 전일 결과':'fast_daily',
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
    return text if text in dict(COMMANDS + LEGACY_COMMANDS) or text in ('paper_results','paper_status','paper_guide','execution','magi3','status1','status2','status3','wave') else None


def main_keyboard():
    return {'keyboard': [[{'text': label}] for label in MAIN_LABELS],
            'resize_keyboard': True, 'is_persistent': True, 'one_time_keyboard': False,
            'input_field_placeholder': '확인할 메뉴를 선택하세요'}


def paper_keyboard():
    return {'keyboard': [[{'text': label}] for label in
                         ('투자결과','투자전략현황','투자전략세부','↩️ 메인 메뉴')],
            'resize_keyboard': True, 'is_persistent': True, 'one_time_keyboard': False,
            'input_field_placeholder': '모의투자 · 확인할 항목을 선택하세요'}


# Stale client buttons repair the bottom keyboard and enter the unified view.
LEGACY_PAPER_BUTTONS = {
    '⚡ FAST 모의투자':'fast', '📊 VPD 모의투자':'vpd',
    '지표가속 모의투자':'indicator', '🧭 전략검증':'results',
    '🧪 shadows 모의투자':'shadow',
}


def clean_markup(markup):
    """Hide obsolete refresh controls, including on legacy message routes."""
    if not markup or 'inline_keyboard' not in markup:
        return markup
    rows = [[b for b in row if '새로고침' not in b['text'] and '다시 조회' not in b['text']]
            for row in markup['inline_keyboard']]
    return dict(markup, inline_keyboard=[row for row in rows if row])


def role_text():
    return ('MAGI 안내·상태\n\n'
            'MAGI1 · 시장 관측\n시세·수급·시장국면 분석\n\n'
            'MAGI2 · 전략 운용\nVPD·FAST·지표가속 등 모의투자와 성과 검증\n\n'
            'MAGI3 · 자산·실행 관리\n실계좌 조회와 Shadow 모의체결 관리')


def role_keyboard():
    return status_keyboard()


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
                               ('MAGI3 · 계좌·실행', 'status3')]] +
        [[{'text':'↩️ 메인 메뉴','callback_data':'nav:menu'}]]}


def scan_keyboard():
    return {'inline_keyboard': [[{'text': '🌅 오전 VPD', 'callback_data': 'nav:morning_scan'},
                                 {'text': '🌙 저녁 VPD', 'callback_data': 'nav:evening_scan'}],
        [{'text': '↩️ VPD 모의투자', 'callback_data': 'nav:vpd'}]]}


def help_text():
    return ('MAGI 메뉴 안내\n\n'
            '/assets — 실투자 현황\n'
            '/paper — 모의투자현황\n'
            '  투자결과: 원금·평가금액·수익률·승률\n'
            '  투자전략현황: 보유·포착 시각·목표·최근 매매\n'
            '  투자전략세부: 포착·매수·매도 방식\n'
            '/regime — 시장국면 (MAGI1)\n'
            '/system_info — MAGI 안내·상태\n'
            '/menu — 메인 메뉴\n/help — 이 안내\n\n'
            'VPD 자동 리밸런싱: 매일 오전 7시 30분 KST\n'
            '수동 운용은 투자전략현황 → VPD/지표가속에서 확인 후 실행합니다.\n'
            '기존 /about · /status도 MAGI 안내·상태로 연결됩니다.')


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
            amount_text=(('<0.1' if 0<amount<0.1 else f'{amount:,.1f}')+f" {r['asset']}") if isinstance(amount,(int,float)) else '전송량 미확인'
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
            '더블볼린저·CCI: 완료 5분봉 BB(20,2)·BB(60,2) + CCI(10) · 별도 300만원·10종목 PAPER\n'
            '장기 밴드폭이 직전 288봉 하위 20%면 6봉 관찰 → 단기 상단 돌파·폭 확대·장기 중심선 상승/유지·CCI>100·거래대금 1.5배 확인\n'
            '신호 확인 후 10초 동안 종가 +0.3% 이내 호가 체결 · 최근 6봉 저점까지 거리 3% 초과면 제외\n'
            '6봉 저점 이탈 또는 단기 중심선 아래 종가와 CCI<0이면 다음 유효 호가로 청산 · 새 수축 뒤 재진입\n'
            '시세 연결이 끊기면 매수를 취소하고 보유분은 연결 복구 후 유효 호가로 청산합니다.\n'
            '전략검증에서 네 전략의 누적·일별 승률과 수익률을 조회합니다.\n'
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
        label = '✅ 확인 · 일괄정리 및 포착정지' if action=='fast_clear' else '✅ 확인 · 포착 및 매매 시작' if action=='fast_start' else '🔴 전량 매도 후 재매수' if action=='rebuild' or action.startswith('indicator_rebuild:') else '✅ 종목 리필' if action.startswith('indicator_refill:') else '✅ PAPER 실행'
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
