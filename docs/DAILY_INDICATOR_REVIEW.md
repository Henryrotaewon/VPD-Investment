# Daily indicator strategy — user design review

Telegram name: **지표가속**, with the main button **지표가속 모의투자**
and `/indicator` entry point. FAST remains a separate future strategy, not a
synonym for this strategy. Existing `fast_*` Telegram commands/callbacks remain
compatibility routes for the current indicator screens; they do not restart FAST.
This naming change does not approve or activate an entry or exit policy.

Supersedes the unagreed minute acceleration trial. Its runtime capture and new
entries are stopped, pending buys canceled, and historical results preserved.
Already-open trial positions retain their stored risk handling; no liquidation
is requested by this change. Start buttons cannot resume the rejected policy.

User intent: detect increasing daily indicator momentum on day D (example: Sep 11)
and enter at the next native daily session start (Sep 12). Evaluate completed
daily bars only. A final D signal is knowable at the D/D+1 boundary, not before it;
data retrieval and execution latency mean an exact opening-price fill is not
guaranteed. No backdated buy is submitted after a restart or delayed scan.

Chart-visible settings: MACD 12/26/9, RSI14 with signal9, Williams %R14, volume
SMA5/10/20, price SMA5/10/20/60/120. RSI signal smoothing, RSI implementation,
band settings and the chart symbol/venue must still be checked. Price MAs are
recorded as context, not silently added to the four-component entry rule.

`magi2.daily_indicator_review.review` calculates the last three completed-day
levels, daily first differences and second differences without looking at future
bars. MACD uses EMA12/26 and EMA9 signal; RSI uses Wilder14 provisionally. RSI
signal9 remains null until SMA/EMA is explicitly specified. Williams remains
signed; moving from -90 to -80 is an increase of 10 percentage points.

The optional composite requires all four scales and weights explicitly. It has
no default weights, no default threshold and never emits a buy decision. Scales
must be chosen using data available by D, then frozen for the comparison. Simple
percentage changes are inappropriate for MACD zero crossings/negative Williams.

Open decisions: universe, normalization/calibration window, weights, threshold,
whether positive acceleration during a still-negative slope is an early signal,
RSI smoothing, entry execution details, and exits. No arbitrary 65/70 thresholds,
overbought bans, 60-minute exit, or TP/SL are inherited into the new strategy.
The review module is not wired to runtime capture, scheduled entries or orders.
Next: retrieve the example symbol's real daily data and inspect Sep 9–11 only,
then evaluate other dates and symbols without tuning solely to the Sep 12 rally.
