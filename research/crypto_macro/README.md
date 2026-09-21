# MAGI1 crypto–macro index research

Status: **offline research prototype; no connected historical panel, measured
predictive result, scheduled collection, live index, or trading integration**.
The existing MAGI1 WAVE records, services, schemas, and raw collectors are not
modified. This is the initial research implementation for the proposed new MAGI1
role, not a claim that WAVE has been empirically disproved.

## Question and outputs

Can Treasury yields, equities, gold/oil futures and crypto-specific information
improve BTC/USD and ETH/USD forecasts at **1 and 7 calendar days**, compared with
crypto momentum/volatility alone? A visually similar price curve is not the
objective function. Evaluate unseen forward returns and the incremental value of
macro inputs on exactly the same observations.

`manifest.json` fixes 15 initial inputs. Every item must have verified historical
availability; source names in the manifest are a sourcing plan, not connected
adapters. It distinguishes Nasdaq Composite from Nasdaq-100, broad dollar from
ICE DXY, futures from spot, and USD from USDT/KRW. Economic surprises and issuance,
halving/upgrade/unlock events are **extended candidates** pending audited coverage.
They can be activated through a separately frozen input manifest. Missing market
consensus must never be replaced with the previous economic observation.

BTC/ETH and horizons have separate fits. Core models are crypto-only, macro-only,
and mixed ridge regressions. Each outer forecast uses labels completed before
its training cutoff, a past-only inner validation block, and train-only scaling.
The inner fit purges labels that overlap the validation interval. Default training
history is at most 1,095 days, minimum 365 inner training rows and 60 validation
rows; refit every 30 calendar days. Alpha candidates are fixed at 1, 10, 100.
Changing these assumptions is a new experiment, not retrospective optimization.

The displayed pressure index is `50 + 50*tanh(predicted_log_return/train_std)`.
It is **not a calibrated probability, expected percentage return, or buy signal**.
50 means zero predicted log return. Standardized coefficients, actual feature
contributions, source times, selected penalties and fit times are exported.

## Data contract

All timestamps are integer UTC milliseconds. Prepare two JSONL files outside the
repository. Do not commit provider data, credentials, or proprietary consensus.

Feature rows:

```json
{"series":"us2y_change_bps","observed_ms":1789689600000,"available_ms":1789762560000,"value":-7,"source":"https://fred.stlouisfed.org/series/DGS2#saved-vintage-reference","quality":"VERIFIED_VINTAGE"}
```

This is a **format example, not a verified observation**. `value` is the feature
already transformed by an audited input producer. The prototype does not yet
implement those source adapters or validate the truth of caller-supplied metadata.

- Yields: percentage-point differences × 100 = basis points; real yields may be
  negative. Curve level = 10-year minus 2-year yield, matched on observation date.
- Equity/dollar prices: returns from completed sessions; use one defined cutoff.
  On weekends/holidays, the most recent actual observation retains its original
  age. No new zero-return observation is fabricated. A seven-day core staleness
  cap is an initial data policy to validate; unusually long closures require review.
- Futures: preserve contract IDs and an advance-defined rollover rule. A raw
  front-contract jump is not an asset return. Negative WTI prices and near-zero
  denominators need an explicit contract-return method; do not take their logs.
- Crypto: trailing seven-day returns and 30-day realized volatility; no centered
  windows or revised future bars. USD pairs must be verified at the source.
- Releases: use the actual public release timestamp, first vintage and consensus
  captured **before** publication. A surprise is unavailable before release. ALFRED
  vintage dates alone do not establish intraday availability. Keep missing consensus
  missing. Scheduled-event countdowns use schedules known then, including revisions;
  do not backdate the realized future halving timestamp.
- `RECORDED_ASOF` means recorded live with receipt time. `VERIFIED_VINTAGE` means
  reconstructed from archived releases with supporting source evidence. The engine
  rejects `LATEST_ONLY` and other unverified labels. Revisions have the same
  observation timestamp and a later availability timestamp. The newest eligible
  observation is selected with its newest **then-known** revision.

Outcome rows (format example):

```json
{"asset":"BTC","quote":"USD","horizon_days":1,"decision_ms":1789689600000,"end_ms":1789776000000,"available_ms":1789776001000,"start_price":100,"end_price":101,"source":"fixture-format-example"}
```

Prices must refer to the exact agreed start/end cutoffs (not a later convenient
bar), from the same USD market. Outcomes are separate from feature observations;
the engine computes log(end/start), checks the exact calendar horizon, and only
uses or scores labels after their availability. No cross-currency substitution.
Only one immutable outcome per asset/horizon/origin is accepted; audit corrected
outcomes in a new dataset version. Input provenance and licensing remain the
responsibility of the data producer.

## Running

The core uses NumPy from the repository's existing requirements.

```bash
python -m research.crypto_macro \
  --features /path/to/audited_features.jsonl \
  --outcomes /path/to/price_outcomes.jsonl \
  --asof-ms <UTC_MILLISECONDS> \
  --output /path/to/research_result.json
```

Optional `--plot /path/to/comparison.png` needs matplotlib. It creates BTC/ETH
one-day observed and predicted price curves, a no-change price baseline, and
return comparisons. Predictions appear at their target end time and were made
one day earlier. Prices share a known starting value, so similar price paths
alone do not prove skill. The return plots and out-of-sample errors are essential.
Seven-day overlapping forecasts are not compounded into a fake portfolio curve.
With no observed results the chart raises a clear error instead of generating data.

The report separates missing/stale inputs from insufficient history. Complete-case
intersections are used for all three model comparisons. `missing_counts` counts
asset–horizon forecast opportunities, not unique economic releases. With no real
input panel the status is `DATA_OR_HISTORY_REQUIRED`, not a neutral score of 50.

## Validation and remaining gates

Tests: `python -m unittest discover -s tests -p 'test_crypto_macro.py' -v`.
Fixtures are **synthetic software tests**, not empirical evidence of predictability.
They test vintage joins, future-data poisoning, delayed labels, purged inner
validation, baseline comparability, currencies, and missing-data refusal.

Current metrics are descriptive out-of-sample MSE, return correlation, directional
accuracy and R² versus the rolling historical-mean forecast. The mixed-minus-crypto
error comparison is paired. They do **not** yet perform block-bootstrap confidence
intervals, multiple-testing adjustment, regime tests or a final untouched holdout.
Those remain mandatory before any adoption; `promotion` is always `NOT_EVALUATED`.

Required next work:

1. Obtain versioned Treasury/index/crypto history; GC/CL contracts plus a frozen
   rollover method; release calendars and pre-release consensus if accessible.
2. Implement and audit transformations, event vintages, session calendars and DST.
   Add age/holiday features only in a separately logged experiment.
3. Freeze training/validation/final holdout boundaries before examining results.
   Evaluate 2020-era institutional change and later market regimes separately.
4. Add block-bootstrap uncertainty (blocks at least the forecast overlap length),
   multiple-testing control, placebo time shifts and leave-one-factor-family-out
   comparisons. Compare with no change, historical mean and crypto-only baselines.
5. Collect forward forecasts without trades. Require stable incremental performance
   on BTC and ETH independently before connecting a MAGI1 derived-record interface.
   Forecast errors are not executable P&L; trading costs belong to a later strategy.

## Research grounding

- [IMF WP 2023/163, The Crypto Cycle and US Monetary Policy](https://www.imf.org/en/publications/wp/issues/2023/08/04/the-crypto-cycle-and-us-monetary-policy-534834): motivates macro/crypto common factors, not a trading-accuracy claim.
- [New York Fed SR 1052, The Bitcoin–Macro Disconnect](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr1052.pdf): short announcement-window findings motivate testing horizons and avoiding a universal causal rule.
- [Liu and Tsyvinski, Risks and Returns of Cryptocurrency, RFS 2021](https://academic.oup.com/rfs/article-abstract/34/6/2689/5912024): crypto-specific momentum/network/attention factors motivate a crypto-only benchmark.
- [FRED real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html): supports the distinction between current revised data and historical vintages.
- [Bitcoin protocol](https://developer.bitcoin.org/devguide/block_chain.html) and [Ethereum issuance](https://ethereum.org/roadmap/merge/issuance/): event definitions must reflect each protocol's different issuance mechanism.

The user-facing research memo dated 2026-09-21 documents the rationale and source
limitations. This prototype has no dependencies on MAGI1 collectors, FAST paper
accounts, exchange order clients or Telegram handlers.
