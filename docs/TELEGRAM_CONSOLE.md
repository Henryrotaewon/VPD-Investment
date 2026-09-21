# 텔레그램 한글 콘솔

`/menu` 또는 `메뉴`로 고정 버튼을 열고 `/help`로 전체 명령을 확인한다. 접두어 없이 `report`, `morning`, `refill`을 쓸 수 있고, 기존 `magi2 ...`, `magi1 morning scan` 등도 호환한다. 전략명 VPD/FAST/WAVE/Whale은 유지한다.

| 명령 | 기능 |
|---|---|
| `/help`, `/menu` | 전체 명령과 한글 버튼 |
| `/report` | 기존 MAGI2 PAPER 자산보고 |
| `/scan`, `/morning_scan`, `/evening_scan` | 저장된 VPD 조회; 신규 스캔/주문 없음 |
| `/status` | 봇 응답과 진행 중 PAPER 작업 |
| `/signals` | 연결된 최신 FAST/Whale 관측; 미연결·만료를 명시 |
| `/strategies` | 비용·비교군·독립표본 중심 검증 기준 |
| `/assets`, `/magi3` | 통합계좌의 미연결 상태 및 MAGI3 구현 수준 |
| `/morning`, `/refill` | 사용자별 일회성 확인 후 PAPER 실행 |
| `/cancel` | 대기 중 확인 취소; 이미 실행된 작업을 중단하지 않음 |

실행 확인은 60초 후 만료되고 다른 사용자나 재사용된 콜백으로 실행되지 않는다. 기존 개인 대화는 설정된 chat ID의 본인만 실행 가능하다. 그룹에서는 `TELEGRAM_ALLOWED_USER_IDS`를 명시해야 PAPER 실행할 수 있다. 조회는 기존 허용 chat 안에서만 처리한다. 라이브 실행 명령은 없다.

하나의 워커가 PAPER 보고/리밸런싱/리필/모니터를 직렬 처리한다. 실행 중 텔레그램 도움말과 상태는 응답하며 추가 PAPER 작업은 거절한다. Telegram polling 프로세스는 하나만 유지한다. `setMyCommands`로 허용 chat에 한글 설명을 등록하며 callback은 `answerCallbackQuery`로 응답한다.

출처: https://core.telegram.org/bots/api#setmycommands · https://core.telegram.org/bots/api#answercallbackquery · https://core.telegram.org/bots/api#replykeyboardmarkup

관측 연결: MAGI1의 `MAGI1_INTELLIGENCE_HTTP_ENABLED=1`과 충분히 긴 `MAGI_INTELLIGENCE_TOKEN`, MAGI2의 동일 토큰 및 `MAGI1_INTELLIGENCE_URL=http://magi1-flow.railway.internal:8081/intelligence`를 설정한다. 공개 도메인은 필요 없다. 로컬 리플레이에서는 `MAGI1_INTELLIGENCE_PATH`가 우선한다. 파일/HTTP 모두 같은 유효기간·스키마 검증을 거친다.


## MAGI 브랜드와 역할별 메뉴

전체 사용자 접점은 MAGI로 통일한다. 시작 시 Telegram의 기본 언어와 한국어에 setMyName, setMyDescription, setMyShortDescription을 적용한다. 사용자명과 봇 토큰은 변경하지 않는다. 프로필 변경 실패는 명령 등록을 중단하지 않으며 비밀값 없이 실패 유형만 기록한다.

/about 또는 🧩 MAGI 역할에서 다음 역할과 조회 버튼을 제공한다.
- MAGI1: 시장 데이터·FAST 후보·WAVE 근거 관측. WHALE은 WAVE 기초자료다.
- MAGI2: VPD 분석·PAPER 모의투자·전략 검증. FAST 재생 도구는 아직 연구 단계다.
- MAGI3: 실계좌 조회·Shadow 검증·실행 관리. 실제 실행 모드는 /status에서 확인한다.

역할 메뉴는 조회만 연결하며 morning/refill 또는 실거래 시작을 연결하지 않는다. /report는 VPD 모의투자, /assets는 실계좌 자산, /shadow는 가상 체결을 구분한다.

### Railway 이름 변경 영향 검토

2026-09-18 확인 기준 프로젝트는 invigorating-charisma, MAGI2 서비스 표시 이름은 VPD-Investment다. 권장 체계는 전체 프로젝트 MAGI, 내부 서비스 MAGI1-Flow / MAGI2 / MAGI3-Execution이다. 이름 변경 영향 질문에 대한 검토이며 이번 코드 배포는 Railway 이름을 변경하지 않는다.

현재 MAGI2 privateNetworkEndpoint는 vpd-investment다. MAGI3 문서의 MAGI2_SHADOW_SIGNALS_URL은 http://vpd-investment.railway.internal:8082/signals다. 서비스 표시 이름을 바꿀 경우 이 엔드포인트를 유지하는지 확인해야 하며, DNS를 바꾸려면 호출측 URL도 함께 바꾸고 인증된 내부 통신을 재검증한다. 프로젝트/서비스 ID, 볼륨, 소스 연결, 토큰, 실행 중단 설정을 보존한다. GitHub 저장소명 VPD-Investment는 별도이며 Pages·백업 경로 등 영향 범위가 있으므로 동시에 변경하지 않는다.

공식 문서: https://core.telegram.org/bots/api#setmydescription , https://docs.railway.com/networking/private-networking

### 기존 채팅 버튼 갱신

`setMyCommands`는 슬래시 명령 목록을 갱신하지만 기존 ReplyKeyboard를 교체하지 않는다.
시작 시 현재 버튼을 포함한 안내 메시지를 한 번 전송하고, 성공한 메뉴 버전·채팅·봇을
영구 상태 디렉터리에 기록한다. 전송 실패 시 성공 기록을 남기지 않는다.
수동 갱신은 `/menu` 또는 `/help`. 기존 자산보고/통합자산/실행상태 버튼 입력도 호환된다.
Railway 서비스 표시 이름은 MAGI2-Strategy, 내부 주소는 기존 vpd-investment를 유지한다.


### VPD 하위 메뉴 (v5)

메인 `📊 VPD 모의투자` 버튼과 `/vpd`는 하위 메뉴를 연다.
`현황 보고`는 기존 `/report`, `리밸런싱`은 `/morning`, `종목 리필`은 `/refill`로 연결한다.
실행 버튼은 사용자 권한 확인 후 60초짜리 일회성 확인을 거치며 즉시 매매하지 않는다.
`메인 메뉴`로 돌아올 수 있다. 이전 직접 실행 버튼 입력도 호환하되 메인 화면에서는 제거한다.
배포 시 v5 버튼 안내를 한 번 보내 기존 채팅의 키보드를 교체한다.

### 관측 메뉴 명칭 정리 (v6)

FAST 후보는 기존 v1 FAST 필터만 표시하며 새 단기 순위 Tracker는 미연결이다.
신호조회는 FAST와 WHALE 합쳐보기여서 메인 버튼을 제거하고 `/signals`와 이전 버튼 입력만 유지한다.
WAVE 근거는 실제 전파 분석이 아니라 온체인 거래 목록이므로 WHALE 참고로 변경한다.
공개 BTC 공급자의 amount는 거래 출력 합계로 표시하고, 미확정 여부와 거스름돈/내부 이동 가능성을 설명한다.
확인되지 않은 지갑 소유자나 거래소 입출금·매매 방향을 추론하지 않는다. 원본 MAGI1 데이터는 변경하지 않는다.

### 메인 버튼 간소화 (v7)

도움말·메뉴 버튼은 메인 키보드에서 제거하되 `/help`와 `/menu`, 이전 버튼 입력은 유지한다.
VPD 조회는 VPD 모의투자 하위 메뉴에서 오전/저녁 저장본을 선택한다. 조회 선택 화면에서 VPD 모의투자로 돌아올 수 있다.

### 전략검증 하위 안내

전략검증 안에서 WAVE/VPD/FAST를 선택한다. 각 안내는 간단 설명·진입/보유/청산 접근·검증 포인트·구현 단계를 표시한다. VPD 숫자는 config.json에서 읽되 기존 포지션은 진입 시 저장 설정을 따른다고 명시한다. WAVE는 연구 가설, FAST는 아직 실시간 미연결 상태로 표시한다. 안내 버튼은 PAPER 작업·실주문·Shadow를 실행하지 않는다.

### WHALE 독립 조회 제거 (v8)

메인·역할 메뉴에서 WHALE 버튼을 제거한다. 기존 `/wave`와 옛 버튼은 WAVE 전략 안내로 연결하고 `/signals`는 FAST만 조회한다. WHALE 수집·저장·원본 스키마는 유지하며 WAVE 보조지표 추가 효과 검증에만 사용한다.

### 응답 지연 개선

5초 고정 대기를 제거하고 Telegram long polling을 사용한다. 25초는 요청을 지연시키는 시간이 아니라 요청이 없는 동안 서버가 대기하는 최대 시간이다. 모니터/점검 기한 전에는 대기를 줄이며 PAPER 작업 중에는 완료/실패를 빠르게 회수한다. Telegram HTTP 연결을 재사용하고 MAGI3 주기 상태 점검은 별도 단일 작업자로 분리한다. 실패 시에만 1초 대기한다. VPD 보고 요청에는 접수 안내를 보낸다.

실제 외부 조회나 보고서 계산 시간은 여전히 필요하고 읽기 명령 자체는 순차 처리한다. command/handler_ms 로그는 서버 처리 시간이며 사용자 체감 종단 지연이나 외부 보고 작업 완료 시간을 뜻하지 않는다.

### Shadow submenu (v9)
- Main button `🧪 shadows 모의투자` / `/shadows`: current assets and recent three-day history.
- `자산현황(현재)` keeps the snapshot age and stale warning; querying never starts trading.
- `최근 3일 매매이력` / `/orders`: rolling 72 hours, KST timestamps, 20 orders per page, stable upper timestamp on subsequent pages. Includes order status and fill amount, fees and realized P&L; unavailable fill values display as unknown, not zero.
- Private `/orders/recent` reads the persistent MAGI3 ledger with inclusive time bounds; legacy `/orders` API is unchanged. Old Telegram Shadow buttons remain aliases.

### FAST automatic observation (v10)
Main and role-menu FAST candidate buttons are removed; `/fast` and legacy `/signals` return monitor status. Four independent public-only workers cover Upbit/Bithumb KRW, Binance USDT and Kraken USD spot universes. Minute price snapshots are kept in bounded memory; every five minutes rank short-term risers. Up to five candidates per venue receive 15-minute L1 observation with a one-second target cycle (not a guaranteed quote/order rate). No raw tape files are created, and MAGI1 WAVE collectors are unchanged.

Initial research thresholds: five-minute gain >=1%, top five or rank jump >=5; fresh 30-second high breakout by 0.05%, spread <=0.15%, no more than 2% chase from candidate selection. These are hypotheses, not validated profitability thresholds. Alert once per candidate window. Price errors/backoff and warmup are exposed in `/fast`. Telegram notification queue is bounded. This service does NOT submit orders or restart Shadow.

`magi3.fast_execution` contains inactive user-run order building blocks: strictly <5% global FAST allocation including positions, pending reservations and estimated fees; complete capital snapshot <=5s; persistent identifiers and unknown-order reservation retention. It is not imported by any deployed runner. IOC Upbit submission and identifier reconciliation are coded against https://docs.upbit.com/kr/reference/new-order and https://docs.upbit.com/kr/reference/get-order; no live API calls were made. Full live controller, automatic sell/fill accounting, strategy ownership reconciliation and integration remain prerequisites. Current partial account valuation must not be used as total equity. User activation is separate; merely changing a mode variable does not make this runtime live-ready.

### FAST verification evidence (v1)
Sparse append-only SQLite events survive MAGI2 redeployments at `/data/magi2/fast_evidence.sqlite3`. No second-by-second book tape is stored. A unique `signal_id` joins CANDIDATE_SELECTED, SIGNAL_DETECTED, ORDER_SKIPPED and WATCH_ENDED; venue/symbol, UTC epoch milliseconds and ISO timestamps, rule version, numerical thresholds and observations are included. Display times remain KST. Quote request duration is measured locally; exchange event time is null when unavailable and must not be treated as network latency. Prices carry quote currency (KRW, USDT, USD).

Signal evidence includes 5/10/15/30-minute returns when available, rank and rank jump, bid/ask, spread, prior high, breakout/chase bps, lookback coverage and watch interval. ALERT_ONLY signals record ORDER_SKIPPED with `submitted=false` and LIVE_EXECUTION_NOT_CONNECTED. No synthetic order/fill is created. Candidate-end records include last observed quote/time and whether a signal fired, allowing non-triggered candidates to be counted.

Inactive execution building blocks append ORDER_REQUEST before HTTP submission, ORDER_RESPONSE on acknowledgement, ORDER_OUTCOME_UNKNOWN on uncertain outcome, ORDER_RECONCILED after identifier lookup, and ORDER_SKIPPED for disabled/risk-blocked requests. They use the same schema in their reservation database. Link records with `signal_id` and `order_id`. Response fields are allow-listed: exchange UUID/state, requested/executed/remaining quantity, actual fees and returned individual fills. An acknowledgement does not mean filled. Tokens, headers and arbitrary response bodies are excluded. Live execution remains unconnected/off.

Export for later analysis (JSONL, no alteration/deletion of history):
`python -m magi3.fast_audit /data/magi2/fast_evidence.sqlite3 --since-ms <UTC_epoch_ms> --until-ms <UTC_epoch_ms>`

These events support detection/order verification, not by themselves a complete profitability backtest: unobserved future prices and slippage cannot be invented. No historical evidence is backfilled from old console lines.

### Venue comparison report
`전략검증 → FAST → 거래소별 신호·오탐 비교` or `/fast_compare` shows a rolling 72-hour report. Counts are deduplicated by signal ID and grouped by venue. The 5-minute non-rising proportion is an explicitly named false-discovery proxy: first valid bid observed 300–305 seconds after detection versus detection ask, return <=0. This includes spread but no fees, is not trading P&L, and is not the statistical false-positive rate among all negative market events. Evaluated sample sizes <30 are marked insufficient; no venue ranking is asserted.

Forward samples are collected only during the existing 15-minute candidate window; observation is not extended. Late detections, restart loss, gaps and historical events lacking a sample are pending until the deadline and then unavailable, never failures or successes. Evaluation version `forward-5m-bid-v1` is separate from detection version `fast-auto-v1`. No older outcomes are reconstructed.

SCAN_COMPLETED records the eligible ranked universe per completed scan. Normalized signal frequency counts only signals joined to their recorded selection scan divided by summed ranked symbol counts, per 1,000 symbol-scans. This avoids assigning pre-instrumentation signals to newly collected denominators; the joined signal count is shown. It does not remove venue differences in market composition or outages. Absolute signal counts are also shown.

### FAST captures and quieter notifications (v11)
Main `⚡ FAST 포착` / `/fast` now displays latest strong captures within rolling 24h, deduplicated by venue-symbol, paged 10 per message. Show detection time/KST, elapsed minutes, initial buy-flow strength, snapshot rise/spread, latest tracked return with age, and five-minute outcome. Expired/restarted tracking is never labeled live. Unmeasured legacy signals are excluded from the strong list but remain in total/evidence counts. Current tracking is in bounded memory, end-of-watch snapshots persist in evidence.

At each price breakout, fetch a bounded public trade sample (Upbit/Bithumb recent 200 ticks, Binance 1,000 aggregate trades, Kraken 1,000 recent trades) and keep trades no older than 30s. Compute buyer-taker quote value / total sampled quote value. Binance buyer-maker=true means sell-taker; Upbit/Bithumb BID and Kraken b mean buy. This is sampled flow, not all-window volume; retain count, time span, last trade age and quote value, no raw trade tape. Require >=20 valid samples, >=5s span, latest <=3s, buyer share >=70%, spread <=10bps, breakout >=10bps and original quote no older than 1.5s after flow confirmation. Missing/slow evidence suppresses notifications. Strength is the observed buy-share percentage expressed on a 100-point scale, NOT a probability.

Append-only ALERT_RESERVED / ALERT_SUPPRESSED events enforce cross-venue same-asset once/hour (XBT normalized BTC), global <=3 per rolling hour and >=10min between notifications. Reservations count even if delivery fails, favoring quiet behavior; suppressed signals still retain original evidence and forward evaluation. Existing broad detector remains fast-auto-v1; notification filter is fast-buy-flow-v1.

No validated per-signal continuation probability exists. UI explicitly says pending. After >=30 evaluated strong signals in the same venue, show historical five-minute continuation rate separately as an empirical reference. Only prior signals with completed outcomes BEFORE the current detection are eligible; no look-ahead. Seven-day history bounds the reference. No percentage is manufactured for unmeasured signals or mislabeled as a forecast.

### Compact real-account summary
`실계좌 자산` / `/assets` renders: total valued assets and current-holdings return; each venue's total, cash and position quantity/value/return/pick strategy; then strategy-group invested cost and return. Technical mode, valuation enum, observation age, raw errors and FX plumbing are omitted.

Return = (sum of current holding values / sum of acquisition costs - 1), excluding cash/quote currencies KRW, USD, EUR, USDT, USDC, JPY and GBP. This is current-holdings mark-to-cost return, not realized/lifetime account performance. Cost/value gaps remain 미확인 and incomplete amounts say 확인분. Unconfigured venues say 미연결; other failed venues say 금액 미확인. Totals summarize connected, valued balances with one plain-language scope note. Stale data is labeled 마지막 조회값 without technical timestamps.

Use real account pick_basis only; existing untagged holdings stay 미분류. Joint strategies remain one composite bucket to avoid double counting; no guessed allocations. Strategy investment amounts are costs of current holdings, excluding cash. Missing basis is never replaced with zero or assigned VPD/FAST/WAVE. Rendering is updated in the MAGI2 deployment using its imported formatter; no account collector or trading-runtime changes are required.

## WAVE analysis views and time-shift controls

`전략검증 → WAVE` and `/wave` now show the rolling 24h research summary, with
신호 강도 / 전파 근거 / 매매 가능성 / 전략 설명 submenus. Four-row pagination
keeps event and edge details readable. This replaces the earlier guide-only
WAVE route; WHALE remains research context without a separate main button.
The MAGI1 read-only background analyzer publishes a private `/wave` snapshot;
MAGI2 caches it so pressing a button does not wait for a network call.
See [WAVE_TIMESHIFT.md](WAVE_TIMESHIFT.md) for the prespecified shifts, quote
coverage exclusions, source-catalog bias and unvalidated trading status.

## Quantity display

All Telegram asset quantities use the shared one-decimal formatter, including
real-account holdings, Shadow holdings/order history and legacy on-chain views.
Positive holdings smaller than 0.1 display as `<0.1` to avoid implying zero.
This is presentation-only; balances, stored JSON, valuation and order precision
are unchanged. Money, prices and return percentages retain their own formats.

## Trading preparation guides

Strategy validation now includes `BASIS · 현선물 준비` alongside WAVE/VPD/FAST.
WAVE and FAST descriptions read the versioned preparation policy; BASIS explains
dated cash-and-carry separately from perpetual funding carry. These are read-only
guides. Opening them never starts execution or a new simulation.
See [WAVE/FAST plan](WAVE_FAST_TRADE_PLAN.md) and [BASIS plan](BASIS_TRADE_PLAN.md).
Pure decision/calculation helpers are not wired to any deployed execution loop.

Strategy validation and WAVE views also provide `크로스마켓 연구`, a read-only
literature-based expansion guide. It describes cross-venue spot/derivative paths
and the additional data needed; it does not render new live opportunities.
See [cross-market research](CROSS_MARKET_RESEARCH.md).

## FAST 모의검증 결과 조회

2026-09-21 이후 `📊 FAST 모의검증 결과` (`/fast_report`)는 **실시간 FAST 모의투자**의
최근 매수 10건을 최신순으로 `거래소·종목 | 매수→매도 시각(KST) | 비용 반영 순수익률`로
간단히 보여준다. 미청산은 보유 중/청산 대기, 10분 초과 여부로 표시한다.
`/fast_balance`는 거래소별 잔고·손익, `/fast_orders`는 최근 10건의 거래 상세를 조회한다.
거래소별 100만원·회당 20만원·기본 5분 시장가 청산·포착 후 10분 청산 기한을
적용하며 `/fast_daily`와 매일 09:00 KST 전일 보고를 제공한다.
기존 재생 실험 조회는 `/fast_replay`로 유지한다. 운영 규칙·비용 가정·청산 지연
표시·저장과 재시작 처리의 정확한 범위는 [FAST PAPER](FAST_PAPER.md)를 따른다.

아래는 종전 재생 결과 조회의 입력 계약이다.

`/fast_replay`는 완료된 업비트 KRW FAST 재생 실험을 조회한다.
조회는 수집·모의매매·Shadow·실주문을 시작하지 않는다.

- 입력: `FAST_PAPER_REPORT_DIR/<run-id>/result.json`과 `upbit-report.json`.
  기본 디렉터리는 운영 상태 디렉터리 아래 `fast`이며, 통상 `/data/magi2/fast`이다.
- 생산자는 기존 `python -m magi2.fast_lab.live_job --output-dir /data/magi2/fast ...`이다.
  최종 `result.json`이 저장된 실험만 대상으로 하며, 완료 시각이 가장 최근인
  업비트 실험 하나를 표시한다. 별도 서비스의 볼륨은 자동으로 공유되지 않으므로
  해당 자료를 MAGI2가 읽을 수 있는 영구 저장 경로에 제공해야 한다.
- 비용과 지연을 적용한 `policy_exit.status=COMPLETE` 거래만 손익·양수 비율에 포함한다.
  미완료·호가 부족·관측 종료는 제외 사유와 건수로 표시한다. 손익은 각 거래의
  `entry.cost_quote * net_return_bps / 10000`으로 계산한다.
- 여러 실험과 통화를 합치지 않는다. 조회 시각과 실험 완료 시각은 다를 수 있으며
  완료 시각을 KST로 표시한다. 파일 누락·형식 오류는 0원 손익으로 대체하지 않는다.
- 재생 실험은 자본 제약 포트폴리오가 아니다. 계좌 잔고·자산수익률·최대낙폭·
  실시간 보유종목·A/B/C 매도전략 비교는 이 조회가 산출하지 않는다.
- 기존 `/fast_compare`는 비용 미차감 5분 호가 변화 평가이며 모의매매 실적과 분리한다.
