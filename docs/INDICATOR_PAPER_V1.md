# FAST indicator acceleration PAPER v1

Replaces the retired WAVE propagation UI and legacy TOP5 runtime. This is a new
paper cohort, not a validated profitability claim or a live-order integration.

## Observation and selection

- Four public spot venues: Upbit/Bithumb KRW, Binance USDT, Kraken USD.
- Each venue selects 20 symbols by reported 24h quote turnover every 15 minutes.
  Price gain is not a universe filter. Existing holdings remain observed.
- Approximately one minute per scan, independent worker/session per venue.
- Frozen completed daily history (at least 100 contiguous days) plus forming day,
  UTC midnight = 09:00 KST. Three intraday observations with 30–180s gaps.
- MACD histogram (12,26,9) normalized by frozen prior close, Wilder RSI14,
  Williams %R14 and linear elapsed-day volume pace versus prior 20-day median.
- Three rising, two accelerating, volume rising and score >=65; RSI>=78 or
  Williams>=-8 prevents entries. Every observation and initial policy is recorded.
- Top five technical candidates: fresh bounded taker sample >=20 trades spanning
  >=5s, latest trade <=3s including confirmation latency, buyer share>=70%, spread
  <=10bps. No forced buy when a venue has no eligible candidate.
- The broad turnover shortlist can miss low-liquidity early movers; raw indicators,
  acceleration thresholds, exhaustion gates and linear volume pace are research
  assumptions, not calibrated probabilities. No BTC adaptive regime is claimed.

## Execution and accounting

Reuse tested quote-only capture-limit accounting, 250ms modeled latency, public
price/lot/minimum rules, 10s entry expiration and capture last price + one tick cap.
Four independent 1,000,000 KRW paper accounts, five slots each, max 200,000 KRW
per slot; foreign FX frozen at initial funding. Partial entry depth releases cash.
No account credentials or actual exchange order endpoint is used.

- Gross price triggers: +12% take profit, independent -6% stop.
- Net estimated profit >=6% activates a 2 percentage-point trailing giveback.
- Score<30 and falling MACD/RSI velocity on two fresh distinct observations exits.
- Maximum holding time 60 minutes. All exit triggers cancel the resting order and
  request execution against a subsequent valid public bid book; depth, fees and
  5bps additional exit slippage apply. Trigger price is not guaranteed fill price.
- No same-venue/symbol reentry after any sale within the 07:30 KST session.
- Automatic first start of the newly authorized paper cohort; later user pause
  persists across deploys. Old FAST ledgers are never opened by this service.

## Evidence and reports

`indicator_paper/indicator-paper-20260925-v1/fast_paper.sqlite3`: immutable trade
entry evidence, fills/events, captured signals, minute valuation observations,
and daily report delivery journal. Daily pages use saved historical valuations;
missing boundary marks are disclosed, never substituted with today's balance.
Full order history has pagination; current holdings include captured score/flow.
MDD is from minute marks only, excluding missing marks and intraminute moves.

`fast_wave/fast-wave-indicator-v1/<cohort>-<venue>/observations.sqlite3`: indicator
observations retained two days, decisions/confirmations/errors eight days.
Trade-linked evidence and the daily journal are not pruned by this routine.

## Retired WAVE data

MAGI1 startup applies `retire-wave-analysis-20260925-v1` before workers start:
remove flow_state, shock_chain, propagation, propagation_missing, reception_trial_v3;
remove FLOW_ONLY/FLOW_VPD/FLOW_VPD_NON_REACTION outcomes; remove flow entries only
when they do not include VPD_ONLY. Preserve unknown cohort schemas, VPD_ONLY,
vpd_snapshot, shared market/quality/onchain data and all raw market files.
Remove only `exports/wave_latest.json` and its known temp filename. Generated
mixed daily Markdown is reduced to its VPD outcome rows, not deleted wholesale.
Shared Drive research snapshots contain both strategies and are preserved.

Production no longer runs FormationEngine/timed WAVE report export or the WAVE
summary publisher; the authenticated `/wave` endpoint returns 410. Existing
Telegram `/wave`, `guide:wave` and old WAVE callbacks open the new strategy guide.
SQLite deleted pages become reusable; no whole-DB VACUUM or immediate filesystem
space recovery is promised. Migration logs include actual per-kind deletion counts.

## Validation

Offline tests cover indicator math and fresh flow, four API candle fixtures,
new isolated ledger, exposure/cash conservation, stale/duplicate/generation gates,
independent stop, trailing protection persisted across restart, weakness confirmation,
maximum holding time, historical marks, observation-to-order pipeline, and scoped
WAVE migration. Deployment logs must additionally verify venue observation health.
