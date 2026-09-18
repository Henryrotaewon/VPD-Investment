# WAVE retrospective catalog controls and Telegram views

Version: `wave-timeshift-catalog-v1`. This is an **exploratory analysis of a
selected episode catalog**, not a validated noise model, causality test, or
trading probability. Motivated by the time-shift background checks in
[the LIGO/Virgo analysis guide](https://arxiv.org/abs/1908.11170); the financial
adaptation and its limitations are our own, not findings of that paper.

## Inputs and protected behavior

The worker reads a consistent, independent SQLite read-only transaction over
`flow_state`, `reception_trial_v3`, `evaluation`, and `diagnostics`. No raw replay,
extra market collector, schema migration, source-row edits or retention change.
Existing FormationEngine, FeatureEngine, propagation, cohorts and timing-quality
functions are untouched. Every five minutes it atomically replaces
`exports/wave_latest.json`; no extra tick archive or unbounded new record stream.
Failure is isolated from collection. A bearer-authenticated `/wave` GET serves
the snapshot separately from the unchanged `/intelligence` contract.

## Comparison definition

* Rolling 24h source-origin window. A completed NORMALIZATION event and at least
  310 seconds since origin are required before an episode can enter controls.
* Catalog: first condition-matching venue timestamp per episode, asset,
  BUY/SELL direction and MICRO/MESO/MACRO horizon, deduplicated by timestamp.
  This is NOT the complete set of independent venue triggers.
* Authoritative actual trials come from reception_trial_v3, deduplicated by
  shock and all edge dimensions. Conflicting statuses/origin times are excluded.
* A hit means a matching follower catalog event at 0–30 seconds after a query
  timestamp. If catalog and actual RECEIVED/NON_REACTION disagree, exclude it.
* Controls shift that timestamp into the past by fixed 11, 17, 29, 43 and 59
  minutes. Offsets are prespecified, not optimized for positive lift. Require
  the same four-hour UTC time block and the same 24h analysis window.
* Both actual and control windows require complete 30-second **quote** coverage,
  established by unions of covered reception-trial intervals for that venue and
  asset. Do not bridge any gap. Coverage of quotes does not imply a complete
  uncensored trigger stream. UNOBSERVABLE never becomes a negative response.
* A case needs at least two covered controls. Compare actual and control rates
  on this same matched subset. Average controls within each case, then average
  cases equally, so cases with more controls do not receive more weight.
* Difference is percentage points. Control-window counts are NOT independent
  samples. All edges are exported; UI ranks by matched sample count, then
  observed sample count, not by best-looking difference. Fewer than 30 matched
  cases are marked insufficient; reaching 30 still means exploratory only.
* Time-resolution checks use only a prior diagnostics snapshot no more than ten
  minutes old and a minimum 1-second floor, in addition to the existing pair
  jitter floor. Unresolved/unknown timing is displayed. Even a resolvable local
  detection lag does not prove venue causality or remaining executable lead.

## What this cannot establish

Formation's global cooldown and first-hit selection preferentially align
within-episode events. Thus a positive difference can arise partly from the
catalog construction itself. Time-block matching does not control for changing
volatility, liquidity, market-wide news, or endogenous sampling. Reused controls
and overlapping shocks remain dependent. No p-values, false-alarm probabilities,
predictive confidence intervals, or corrected discovery claims are published.

Next inference-grade dataset must record independently selected venue events
and feature-observation coverage, with pre-event market-state controls, followed
by held-out chronological evaluation and cluster-level uncertainty. Increasing
the old catalog's sample count alone does not fix its selection bias.

## Telegram contract

`전략검증 → WAVE`, legacy `guide:wave`, and `/wave` open a summary plus:

* **신호 강도**: newest detections, initial signed price return, signed net
  aggressor-volume ratio, prior-window volume multiple and spread. A volume
  multiple of zero in the source means the baseline can be unavailable, so
  show 기준 부족. No heuristic-to-probability conversion or invented anomaly score.
* **전파 근거**: observed/unobservable counts, matched case count, control-window
  count, actual vs control response and difference, local lag and timing-qualified
  count. Four edges per page; all six venues can be origins/followers.
* **매매 가능성**: explicitly unvalidated. Separate descriptive legacy Upbit BUY
  quote-v2 FLOW_ONLY outcomes at 10/30/60/300 seconds, deduplicated by shock and
  horizon; other overlapping cohorts, legacy results and SELL are excluded.
  Window is evaluation completion time. Quotes include entry ask/exit bid, but
  fees, order latency and depth slippage are excluded. These are initial-detection
  outcomes, not entries after multi-venue confirmation and not actual fills.
* **전략 설명** retains the user-facing strategy guide.

MAGI2 refreshes a cache in the background every 30 seconds. Button handlers perform
no remote request or analysis. A failed fetch preserves the last report, visibly
marked after its 15-minute expiry. New cache accepts no future/expired snapshots
or execution-eligible payloads. Querying does not execute PAPER, Shadow or live
orders and does not send unsolicited WAVE alerts.

## Verification

Tests exercise known excess and null catalogs, per-case weighting, missing
coverage, conflicts and duplicates, separated direction/horizon, time boundaries,
unfinished/future data, timing ambiguity, source immutability, authenticated GET,
expired contracts, cache-only navigation and absence of trading calls.
Run the full MAGI1 suite plus the main and MAGI3 suites before merge.
