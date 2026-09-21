# FAST TOP5 entry tick filter

User-selected rule: exclude a new paper buy when one exchange price tick is
**6% or more** of the executable best ask. Exactly 6% is excluded; values below
6% are allowed. This is a user-selected risk constraint, not a fitted optimum.

The current RankLedger enables the filter for Upbit, Bithumb, Binance and Kraken.
After validating a fresh entry book and before any simulated fill, the ledger
looks up the exchange tick at the best ask and compares `tick * 100 >= ask * 6`
with Decimal arithmetic. The price excludes synthetic slippage. It uses the
existing rules/cache and entry book, so the filter adds no HTTP requests.

For the reported Bithumb BTT case, a 0.0001 KRW tick at a 0.0005 KRW ask is 20%:
the entry is skipped, its reservation is released, and account cash is unchanged.
The skip reason, reference price, tick size, percentage, threshold, rules source
and book timestamp are stored. The report shows `1틱 20.00% · 6% 이상 매수 제외`.
Runtime startup logs expose the enabled threshold and skip transitions include
the measured tick percentage.

Raw TOP5 rankings are preserved: skipped members are not replaced with rank 6
or below. Existing rank episode consumption rules apply to skipped entries.
The filter itself does not change existing holdings or exit rules. +12% exits
and TOP5-exit AND -6% stops continue to apply.

The user subsequently requested a fresh trial. One-time reset marker
`fast-top5-tick6-fresh-20260922-v1` removes only the existing FAST paper/evidence
SQLite files and their WAL/SHM companions before workers start. It clears old
FAST captures, positions, trades, ranks and paper balances, then seeds each of
the four venues with KRW 1,000,000 equivalent and resumes scheduled scans.
The marker prevents repeating the reset on later restarts. VPD, MAGI1 and MAGI3
data and real balances are untouched. No live-order changes are included.

The rule limits price-grid coarseness at entry. It does not limit spread, future
price gaps, or maximum realized loss to 6%.
