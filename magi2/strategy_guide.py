"""Read-only strategy explanations; opening a guide never authorizes execution."""
import json
from pathlib import Path
from magi2.trade_plan import strategy_plan_text
from magi2.basis_plan import basis_text
from magi2.cross_market import guide_text as cross_market_text


def strategy_keyboard(detail=False,fast=False):
    rows=[[{'text':name,'callback_data':'guide:'+name.lower()} for name in ('WAVE','VPD','FAST')]]
    rows.append([{'text':'⚖️ BASIS · 현선물 준비','callback_data':'guide:basis'},
                 {'text':'🌐 크로스마켓 연구','callback_data':'guide:cross'}])
    if fast:
        rows.append([{'text':'📊 FAST 모의검증 결과','callback_data':'nav:fast_report'},
                     {'text':'📅 전일 결과','callback_data':'nav:fast_daily'}])
        rows.append([{'text':'📊 거래소별 신호·오탐 비교','callback_data':'nav:fast_compare'}])
    if detail:rows.append([{'text':'↩️ 전략검증','callback_data':'nav:strategies'}])
    rows.append([{'text':'↩️ 메인 메뉴','callback_data':'nav:menu'}])
    return {'inline_keyboard':rows}


def strategy_text(name):
    if name=='cross':
        return cross_market_text()
    if name=='basis':
        return basis_text()
    if name=='fast':
        return ('⚡ FAST · +12% 익절 / −6% 손절 모의투자\n\n'
                '5분 전 대비 +5% 이상 포착 조건을 유지합니다. 1분 간격 관측, 거래량·호가 차이 필터 없음.\n'
                '거래소별 가상자금 100만원 · 종목당 20만원 · 최대 5종목.\n'
                '포착 후 시장가 모의매수하고 평균체결가의 112% 이상 유효 호가에 지정가 매도 주문을 냅니다. '
                '매수호가가 평균매수가 대비 −6% 이하에 도달하면 지정가를 취소하고 시장가로 손절합니다. '
                '가격 기준이며 수수료·슬리피지는 별도 반영합니다. 시간제한이나 반복 재매수는 없습니다.\n'
                '매도한 거래소·종목은 매도 당일 KST 자정까지 재매수하지 않습니다. 다음 날 신규 포착부터 허용합니다.\n'
                '/fast_clear 또는 FAST 정리 버튼: 모든 거래소 FAST 보유 전량 시장가 청산, 미체결 매수 취소. '
                '실제 체결 가능한 호가·잔량으로 모의처리하며 자료 부족 시 청산 대기로 남깁니다.\n'
                '/fast_report 최근 10건 · /fast_orders 상세 · /fast_balance 잔고\n'
                '/fast_daily 매일 09:00 KST 전일 보고\n'
                '사용자 요청에 따라 FAST 이력·잔고를 한 번 초기화합니다. 이후 재시작 시 보존합니다. 실제 주문 없음.')
    if name=='wave':
        return strategy_plan_text(name.upper())
    if name=='vpd':
        cfg=json.loads((Path(__file__).with_name('config.json')).read_text())
        top=cfg['session']['top_n']; hold=cfg['hold']; exits=cfg['exit']
        return (f'📊 VPD · 상위 후보 분산 모의투자\n\n'
                '간단 설명\nVPD 점수와 모멘텀 등 스캐너 분석으로 후보를 선별하고, 보유 신호가 유지되는 동안 운용하는 전략입니다.\n\n'
                '매매 공략 · 현재 PAPER 규칙\n'
                f'① 최신 VPD TOP{top}을 기준으로 신규 빈자리에 균등 금액으로 진입합니다.\n'
                '② 보유분은 TOP10 여부와 별도로 상승 모멘텀과 평균 이상 거래대금 중 하나(3일·10일 기준)를 함께 확인합니다. 점수가 낮아도 기존 수익과 상승 추세가 유지되면 수익 보호 조건으로 보유할 수 있습니다.\n'
                f'③ 신호 약화는 서로 다른 자료 {hold["weakening_confirmations"]}회 이상, 최초 약화부터 {hold["weakening_grace_hours"]}시간 이상 지속할 때 청산합니다. '\
                f'신규 포지션 설정은 순수익률 {exits["take_profit_pct"]:+g}% 익절 / {exits["hard_stop_pct"]:+g}% 손절이며, 기존 보유분은 진입 당시 저장된 설정을 따릅니다.\n'
                f'수익 보호 모의 기준: 관측 순수익이 +{exits["profit_protection_activation_pct"]:g}%에 도달한 뒤 최고 수익에서 {exits["profit_protection_giveback_pct_point"]:g}%p 되밀리면 청산합니다. 이 값은 최적화된 수치가 아닌 검증용 가정입니다.\n'
                '④ 리밸런싱은 보유 종목 재조정, 종목 리필은 기존 보유분을 팔지 않고 빈자리를 채우는 기능입니다.\n'
                '⑤ 전량 교체는 별도 확인 후 기존 보유를 모두 매도하고 최신 VPD TOP10으로 균등 재구성합니다. 보유·당일 재진입 유예를 해제하므로 같은 종목도 재매수될 수 있습니다. 원금·누적 손익·원장은 보존하고 모의 매매 비용을 반영합니다.\n\n'
                '운용 시간\n24시간 언제든 리밸런싱을 요청할 수 있습니다. 요청 시점 직전까지 완료된 분봉 기준으로 전 종목을 같은 시각에 맞춰 새 VPD를 생성하고 재평가합니다. 09시에는 전일 확정 일봉, 그 외에는 해당 시각까지의 당일 흐름을 반영합니다. 같은 분의 자료로 매매를 반복하지 않고 당일 청산한 종목은 재매수하지 않습니다. 스캔 중에도 익절·손절·수익 보호 모니터는 계속 작동합니다.\n\n'
                '검증 포인트\n수수료·슬리피지 차감 손익, 최대 손실폭, 보유 기간과 순위 교체 효과를 확인합니다. 실제 수익률은 VPD 모의투자 → 현황 보고에서 조회하세요.\n\n'
                '현재 단계\nPAPER 운영 중입니다. 실제 계좌 자산·MAGI3 Shadow와는 별도이며 실거래 수익을 의미하지 않습니다.')
    raise ValueError('UNKNOWN_STRATEGY_GUIDE')
