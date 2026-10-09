# Telegram integrated PAPER navigation and VPD schedule

Authorized menu update and daily VPD PAPER rebalance, 2026-10-10 KST.

## Navigation

The persistent top menu has three buttons in this order: 실투자 현황, 모의투자현황,
시장국면 (MAGI1). PAPER opens 투자결과, 투자전략현황 and 투자전략세부.
Six active independent strategies share the same navigation: VPD, FAST base,
Indicator, Double Bollinger/CCI, FAST-BEAR and FAST-DERIVATIVES. MAGI3 Shadow
remains available as a separate reference, because its fills and account basis
differ. Missing data is reported as pending rather than zero performance.

Results reuse existing ledger calculations and show capital, valuation, return,
win rate and timestamps. FAST keeps its existing restart equity anchor and
excludes earlier trades. Position views show captures separately from entry,
strategy-specific targets and latest fills/realized PnL. VPD historical capture
time is not invented: only its stored first-selection date is displayed.
Opening these pages only reads ledgers; no account is created or reset.
Legacy commands remain accepted. Manual trading confirmations remain unchanged.
Refresh buttons are removed at the Telegram send boundary, including legacy
views. Each selection reads available current data; messages are not live-edited.
Keyboard v23 replaces the old persistent client keyboard once after deployment.

## Automatic VPD rebalance

MAGI2's existing event loop checks for 07:30 Asia/Seoul daily. A fresh point-in-time
scan runs first, followed by the existing `morning` PAPER rebalance. This does not
perform `rebuild`, clear capital/history or change entry/exit thresholds.
The 09:00 KST statistical trading-day boundary is unchanged.

A busy worker/server recovery can defer dispatch until 07:45 (exclusive). Missed
older schedules are not replayed later. An SQLite unique claim in the persistent
volume prevents two workers/restarts from starting the same scheduled date.
The claim is committed before dispatch; a crash after claim does not silently
retry potentially completed trades. Existing request records track claimed,
executing, completed or failed outcomes. Existing scan/execution notifications
report results. Manual requests retain their separate explicit confirmations.

First eligible scheduled start after the midnight deployment: 2026-10-10 07:30
KST (2026-10-09 22:30 UTC). Completion follows the scan and is not guaranteed at
07:30 precisely. Runtime startup logs announce the schedule without triggering
an immediate rebalance outside the window.

## v24 correction: bottom keyboard hierarchy

The user's post-v23 screenshot still showed the old nine-button bottom keyboard.
The v23 send was accepted, but that did not establish that the client displayed
the new keyboard. v24 explicitly sends `ReplyKeyboardRemove` and then the new
`ReplyKeyboardMarkup`, recording success only after both requests succeed.
The migration marker and runtime log now include the actual button labels.
See Telegram's [ReplyKeyboardRemove API](https://core.telegram.org/bots/api#replykeyboardremove).

The main bottom keyboard contains 실투자 현황, 모의투자현황, 시장국면 (MAGI1),
and the combined MAGI 안내·상태. Selecting 모의투자현황 replaces the bottom
keyboard with 투자결과, 투자전략현황, 투자전략세부 and ↩️ 메인 메뉴.
Strategy selectors and pagination remain attached to their result messages.
Pressing a stale FAST/VPD/Indicator/Shadow/strategy button also replaces the bottom
keyboard and routes to the new PAPER hierarchy. Explicit legacy commands and
confirmed manual execution remain supported.

`about`, `status` and `system_info` now open one combined role/state page. Network
checks run in a separate worker, so waiting for MAGI1/MAGI3 does not block the
Telegram polling loop or trading workers. Repeated requests share one pending
job. Missing connections remain labeled unavailable. The slash menu advertises
only the four top-level entries; old help/menu commands still resolve.

Regression coverage includes clear-then-replace order, failure of the second
send with retry, persistent parent/child/back button transitions, stale-button
migration, merged asynchronous status and unauthorized callbacks.
