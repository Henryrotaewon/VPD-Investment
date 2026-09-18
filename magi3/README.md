# MAGI3 구현 상태

2026-09-18 코드 확인 기준. **실거래 미가동, 구성요소 기반 단계**다. runner는 설정을 출력하고 종료한다. 배포 SUCCESS는 상시 주문 엔진 가동의 증거가 아니다.

| 영역 | 구현 수준 |
|---|---|
| StrategySignal | 생성·파싱 계약 있음; 상시 소비/검증/라우팅 미연결 |
| 4개 거래소 | Upbit/Binance/Kraken/Bithumb 공개 호가 호출 및 공통 최우선호가 정규화 |
| 개인 계좌 | Upbit JWT/Binance 서명 조회 코드; 실제 연결 검증 별도. Kraken/Bithumb 서명 미구현 |
| Shadow | 제공된 호가를 걸어 체결·수수료·슬리피지를 계산하는 함수와 별도 원장 있음 |
| 위험관리 | 주문금액·총노출·일손실·kill switch 검사 함수; 실계좌 상태 기반 연결 미완료 |
| 실행관리 | 실주문 제출·중복주문 방지/체결 대사·재시작 복구·자동 청산 루프 미구현 |
| 보고 | 입력된 KRW 정규화 포트폴리오로 총자산/거래소/종목 PICK/전략별 평가손익 계산 |
| 보고 데이터 | 실계좌 자동수집·환율 정규화·수수료 포함 실현손익 연결 미구현 |
| 전략 배분 | 복합 태그는 원가·평가손익을 균등 분배. 독립 전략의 실거래 성과로 해석하지 않음 |

Defaults: `MAGI3_MODE=DRY_RUN`, `MAGI3_LIVE_ENABLED=0`, `MAGI3_KILL_SWITCH=0`, max order/total exposure 30,000 KRW, daily loss 10,000 KRW. These are configuration defaults, not verified live-account risk enforcement.

Run configuration diagnostic: `python -m magi3.runner`.
Run a synthetic report: `python -m magi3.report_cli --input magi3/sample_portfolio.json`. The sample contains fictional balances.

Upbit `order_test` uses a non-executing test endpoint. `submit(live_allowed=True)` deliberately raises `LIVE_ORDER_PATH_NOT_IMPLEMENTED`. No withdrawal implementation exists. Never turn on live trading based only on these configuration flags or unit tests.
