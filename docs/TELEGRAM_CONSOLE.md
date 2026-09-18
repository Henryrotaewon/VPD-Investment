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
