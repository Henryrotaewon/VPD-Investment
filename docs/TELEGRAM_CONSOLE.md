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
