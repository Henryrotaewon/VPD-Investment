# FAST entry experiment v1

Existing fast-flow-paper-v2 keeps its original ledger, policy and daily BUY lockout.
Two prospective independent accounts begin together with KRW 3,000,000 each:
control requires two consecutive qualified 10-second windows; early requires one.
Both use the exact same completed 5-minute reference, ten historical same-time days,
70-second live warmup, freshness, flow thresholds, five active capture tracks,
10 portfolio slots, 1.5% signal-price cap, 10-second maximum wait, costs and exits.
An unfilled signal must reset before retry; filled symbols cannot re-enter that Upbit day.
The paired control has its own start date and daily state, so it is not the old account's
cumulative P&L. No historical signals are replayed. Partial 5-minute extrapolation is
not part of this experiment: only confirmation count changes.

Ledgers: fast-observe/entry-experiment-v1/{control,early}.sqlite3.
EXPERIMENT_SIGNAL events retain first-condition timestamp/price, confirmation delay,
price change, features and protection. Fills retain decision and execution timestamps.
Pair entries by symbol and Upbit day; also retain early-only and control-only outcomes
rather than reporting only the intersection. Compare net equity, normal exits,
DATA_GAP exits separately, win/loss counts and entry timing. Early-only losses are
part of the experiment, not excluded false positives. A few trades do not establish
profitability; collect normal completed trades before changing the main strategy.

Telegram FAST balance shows both net results; status.json contains both reports.
All ledgers receive identical public feed lifecycle events. Restarts preserve history,
but inherited gap policy exits held positions on the next fresh book. Deploy when
main has no held or pending positions; do not reset the main ledger.
