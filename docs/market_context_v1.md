# MAGI1 시장국면 자료구조 v1

MAGI1은 5분마다 공개 현물 자료를 수집한다. MAGI2의 기존 시장 국면 버튼 및 `/regime`은 인증된 `/market-context`를 조회한다. 과거 90일 Kraken 가격 분류는 테스트·비교 코드로 남지만 조회에서는 사용하지 않는다. 자동 자금배분이나 매수·매도 조건 변경은 포함하지 않는다.

## 자료 및 시간 기준

- 업비트 KRW 활성 비유의 종목: 시장목록, 현재가, BTC/ETH 시간봉·일봉.
- Binance USDT 활성 현물: exchangeInfo, UTC tradingDay MINI ticker, BTC/ETH klines. 접근 제한·오류는 해당 거래소 판단 보류로 표시하며 다른 거래소 가격으로 대체하지 않는다.
- 당일 수익률 = 현재가 / UTC 당일 시가 - 1. 전일종가 대비 API 등락률이나 rolling 24h를 대용하지 않는다. UTC 00시는 KST 09시다.
- 1/4/24시간 수익률은 완료 시간봉 종가끼리, 7/30일은 완료 일봉 종가끼리 비교한다. 진행 중 캔들은 제외한다. 연속 25개 시간봉·31개 일봉이 필수다.
- 메이저는 BTC·ETH로 고정. 안정화폐·금 연동 토큰은 알트 분모에서 제외한다. 목록은 코드 EXCLUDED 및 스냅샷에 기록한다. 거래대금 비중은 시총 도미넌스가 아니다.
- 전체 대상 중 유효 시세 80% 이상, 알트 20종 이상, BTC·ETH 확보를 요구한다. 시세 5분 경과·가격 오류·미래 시각(30초 허용치 초과)은 제외한다.
- FRED SP500/NASDAQCOM/DGS10/DTWEXBGS/DCOILWTICO를 6시간 캐시한다. 공표 자료의 관측일, 수신시각, 변화 단위를 보존한다. 전 관측치 대비 변화이며 당일 실시간 변화가 아니다. 금리는 %p, 나머지는 비율이다. 7일 초과 관측치는 STALE. 휴장/공표 지연 자체는 중립 신호로 간주하지 않는다. 거시 참고 자료는 현물 방향 판정에 합산하지 않는다.

## 스냅샷 계약

`schema_version=magi1-market-context-v1`, `policy_version=regime-observation-v1`, `producer=MAGI1`, `asset_class=CRYPTO_SPOT`, `mode=OBSERVATION_ONLY`, `execution_eligible=false`.

`observed_ts_ms`(수집 시작), `generated_ts_ms`(완료), `expires_ts_ms`(관측+10분), `day_start_ts_ms`, `snapshot_id`를 기록한다. 거래소별 `source_metadata`는 통화·시간대·달력·출처를 보존한다. 각 거래소의 상태는 독립적이며 누락은 UNAVAILABLE, 값은 0으로 대체하지 않는다. 다음 거래일이 시작되면 직전 거래일 스냅샷은 유효기간 안이어도 소비자가 거부한다.

`venues[venue]`에는 중기/단기/당일 및 종합 방향, 확산 상태, 위험 관측, BTC/ETH 수익률·실현변동·당일 고점 대비 하락, breadth(분모/분자/커버리지/상승 비율/BTC 초과 비율/중앙값/메이저 거래대금 비중), 사유, 2회 연속 확인 상태가 들어간다.

주식 추가 시 별도 asset_class·거래소 달력·세션·통화로 수집기를 추가한다. 공통 거시자료와 소비 계약은 재사용할 수 있다. 현재 국내외 주식 개별 시세 수집기는 포함하지 않는다.

## 초기 관측 기준 — 수익성 미검증 가설

- 종합 방향: BTC 당일 ±0.3%, 완료 1시간 ±0.25%, 완료 4시간 ±0.5% 중 같은 방향 2개 이상. 그 외 혼조.
- 중기: 완료 7일 ±1%; 단기: 완료 4시간 ±0.5%; 당일: ±0.3%.
- 알트 확산: 알트 상승 비율 60% 이상, BTC 초과 비율 50% 이상, 중앙 수익률 +0.2% 초과.
- 메이저 집중: BTC·ETH 거래대금 비중 35% 이상, 또는 BTC 상승+초과 비율 40% 미만+알트 중앙값 BTC 미달.
- 동반 약세: 상승 비율 35% 이하 및 중앙 수익률 -0.2% 미만. 위 순서대로 평가하며 나머지는 혼재. 방향과 확산을 독립 축으로 유지하므로 하락/알트확산 조합도 가능하다.
- 위험 높음: BTC 시간 로그수익률 표본표준편차×√24가 4% 이상, 또는 당일 고점 대비 -3% 이하.
- 방향/확산 조합이 4~10분 간격으로 두 번 일치해야 확인. 중복 조회·누락·오류 구간을 연속 표본으로 인정하지 않는다. 최초 표본은 관측 상태다. 위험 경고는 확인 대기를 적용하지 않는다.

## 저장·장애 처리

`exports/market_context_latest.json`을 임시 파일 후 원자 교체한다. 별도 `market_context.sqlite3`에는 전체 종목 원천 틱/캔들 대신 요약만 저장한다. 최근 7일은 5분 단위, 이후 90일까지 정시 표본만 유지한다. SQLite 삭제 페이지는 후속 기록에 재사용한다. 기존 원천 데이터 보관 정책과 분리된다.

수집은 독립 비동기 작업에서 blocking HTTP를 별도 스레드로 처리한다. 기존 수집 큐와 보호 모니터를 막지 않는다. 조회는 이전부터 사용하는 내부 인증을 재사용하며 토큰이 없으면 보류한다. 10분 초과·미래·다른 거래일·스키마 오류 자료로 분류하지 않는다. 외부 API 호출 실패는 다음 5분 주기에 재시도한다.

## 근거 문서

- https://docs.upbit.com/kr/reference/list-candles-minutes
- https://docs.upbit.com/kr/reference/list-candles-days
- https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- https://fred.stlouisfed.org/series/DGS10

이 자료는 API 의미와 관측 출처의 근거다. 위 수치 기준의 투자 성과를 입증하지 않는다.

## Binance 응답 계약 보완

tradingDay는 symbol 또는 symbols가 필수이며 최대 100종목씩 요청한다. closeTime은 통계 구간의 종료시각(당일 23:59:59.999일 수 있음)으로 마지막 체결 시각이 아니다. UTC 당일 openTime/구간 경계를 검증하고 거래소 서버 시계(30초 오차 한도)와 HTTP 수신으로 조회 신선도를 확인한다. 마지막 체결 신선도까지 확인된 것으로 표시하지 않는다. 업비트는 trade_timestamp를 사용한다.
