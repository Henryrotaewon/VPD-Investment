# Indicator PAPER clean start — 2026-10-22

The user explicitly requested a fresh indicator experiment: discard its current
holdings, pending orders, erroneous records and daily aggregates, then resume.
This is a separate authorization from the earlier storage-efficiency work.

## Diagnosis and repair

The repeated `indicator_risk_fill_error` previously logged only `ValueError`.
The hourly scan at 22:00 KST calculated 259 of 298 markets; 36 lacked 100 daily
candles and three had no trade in the last hour. Those eligibility exclusions
do not establish corrupt RSI, Williams or MACD inputs.

The old fill query requested the latest 200 five-minute candles. An older exit
outside that window raises `FILL_WINDOW_MISSING` forever as the window advances.
The repair requests windows starting at the original earliest fill time, each
containing at most 200 possible bars. Empty/no-trade windows are walked forward;
the first actual candle is used, with no fabricated prices or gaps. Short windows
request only the needed candle count. The existing order-time and duplicate-fill
guards remain in force. Upbit documents exclusive `to`, maximum count 200 and
omission of no-trade candles:
https://docs.upbit.com/kr/reference/list-candles-minutes

Fill errors now include the failing stage, specific reason, market and order
timestamps. Hourly exclusions include up to three affected markets per reason.

## Reset contract

`MAGI2_INDICATOR_RESET_ONCE=indicator-cleanstart-20261022` explicitly enables the
one-time operation before indicator workers start. It first logs current counts
and reproduces the previous 5m request for each pending order without executing
it. Those production diagnostics distinguish the confirmed failure from the
reproduced code defect. Network failure is reported and does not prevent the
explicitly requested reset.

Only the current `indicator_paper/<COHORT>/ledger.sqlite3` is changed: state,
events and marks are replaced/cleared in one transaction. This clears holdings,
orders, realized P/L, signal confirmations, observations, quote caches and both
daily performance views' source rows. Fresh PAPER capital is 3,000,000 KRW,
enabled with zero holdings/orders and a new start time. Past orders are discarded,
not recorded as invented sells. Entry still needs two fresh hourly observations.

The durable receipt is in the same state transaction, so redeploying with the
same environment setting cannot reset new trades again. A failed transaction
preserves the old state/history. VPD, FAST, Bollinger, MAGI1 and MAGI3 data are not
opened by the reset. No remote archive, database transfer or VACUUM is required;
SQLite pages become available for reuse. Historical Railway logs are retained.

## Verification

68 targeted tests pass: historical first-fill recovery, sparse markets, reduced
short-window payloads, malformed/future candle rejection, reset isolation,
transaction rollback, restart idempotency, detailed error logs, hourly signals,
independent risk exits, rebalance and performance views. CI runs the new tests.

Operational completion requires the new deployment to succeed, the reset receipt
log to report zero positions/pending/events/marks, and subsequent worker logs to
show the new account. A queued deployment is not a completed reset. Remove the
one-time environment request after that confirmation without redeploying.
