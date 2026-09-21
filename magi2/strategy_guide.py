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
        return ('⚡ FAST · 10분 1틱 반복 모의투자\n\n'
                '업비트·빗썸·바이낸스·크라켄 각 최초 100만원, 종목당 20만원, 최대 5종목입니다. '
                '현재가가 5분 전 대비 +5% 이상인 종목을 1분마다 포착합니다. 거래대금 배수·상승 가속은 제외 조건이 아닙니다. 기준 가격의 시각 차이는 5분±15초까지 허용하며 실제 비교 간격을 기록합니다. '
                '최소 호가 차이는 포착 제외 조건으로 사용하지 않습니다.\n\n'
                '포착 후 현재가에 지정가 매수 → 매수 체결 직후 현재가+1틱에 지정가 매도 → 매도 완료 후 다시 매수합니다. '
                '매도 기준은 매수가가 아니라 그때의 현재가입니다. 주문 가격은 임의로 따라 바꾸지 않습니다. '
                '부분 매수분이 최소 매도금액을 충족하면 남은 매수를 취소하고 바로 매도로 전환합니다.\n\n'
                '최초 포착 시점부터 10분이 되면 반복을 중단하고 미체결 주문을 취소한 뒤 보유 잔량을 시장가 모형으로 청산합니다. '
                '반복하거나 재시작해도 청산 기한은 연장하지 않습니다. 자료 단절·잔량 부족·최소주문 미달 시 지연을 표시합니다.\n\n'
                '공개 호가의 앞선 대기 물량과 반대 방향 체결량으로 모의 체결을 판단합니다. 가격 접촉만으로 체결하지 않습니다. '
                'REST 관측 간격·취소/숨은 물량 때문에 실제 체결 순서는 정확히 재현할 수 없습니다. '
                '왕복 수수료가 틱 차익보다 클 수 있으며 강제청산에는 호가 깊이와 추가 슬리피지 0.05%를 반영합니다.\n\n'
                '/fast_report 최근 10세션 시각·왕복 횟수·순수익률(배정 20만원 기준)\n'
                '/fast_balance 거래소별 잔고·버전별 손익\n/fast_orders 주문·수수료·강제청산 상세\n'
                '/fast_daily 매일 09:00 KST 전일 보고\n\n'
                '원금·기존 이력·해외 고정 환산율은 유지합니다. 사용자 요청으로 기존 FAST 포착·거래원장·가상잔고를 초기화했고 v4로 새로 시작했습니다. 이후에는 재시작해도 원장을 보존합니다. '
                '실시간 PAPER · 실제 주문 없음.')
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
