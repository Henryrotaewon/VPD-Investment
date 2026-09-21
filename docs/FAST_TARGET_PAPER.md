# FAST target/stop paper v5

User-authorized replacement of execution only; detector stays fast-price-rise-v4 (5-minute +5%, 60-second polling). No live exchange orders.

- Four venue accounts, KRW 1,000,000 equivalent each; 200,000 per position, five positions per venue.
- Immediate paper market entry against a fresh public ask book after 250 ms latency; depth, quantity filters, fees and 5 bp additional slippage apply. Entry evidence expires after 10 seconds; missing/depleted books do not invent fills.
- Average execution price (before fees, including simulated impact) fixes a 112% target, rounded UP to a valid exchange tick. Sell limit is persisted in the same transaction as the entry.
- Best executable bid at/below 94% of the fixed entry average triggers cancellation and market liquidation. Gaps, fees and impact can make net loss exceed 6%.
- No timed exit, no repeated buys. Target fills use the existing conservative queue/aggressor-tape model. A target outside visible depth rests until depth becomes observable; no fictitious queue/fill.
- Each sale, including a partial sale, blocks that venue/symbol from entry through KST midnight. The block is derived from durable fills and survives process restart. Next-day fresh signals may enter; holdings continue to block duplicate entry.
- Authorized `/fast_clear` and `nav:fast_clear` cancel pending entries and resting sells across all four venues and persist market exit requests. Workers recover incomplete exits after restart. A chat/actor-bound one-use confirmation expires in 60 seconds; only its explicit Confirm action liquidates. Cancel, expired, replayed and unauthorized callbacks do nothing. Existing chat and actor execution checks remain. Future other-symbol signals remain active.
- Daily 09:00 KST reports and recent-ten views retain actual net costs; return denominator is allocated slot capital.

One-time reset `fast-target-v5-20260921` removes only FAST evidence/paper SQLite and sidecars before workers start, then reseeds. Future restarts preserve v5 history and account state. VPD and other services are unchanged.

Validation covers target rounding, market entry evidence, fixed target/no time exit, profitable target exit, stop boundary, fee/slippage reconciliation, restart/day rollover, partial market exit recovery, four-venue clear, tape-independent stop, reports and authenticated command/button routing.

## Holdings result view

The FAST result button shows all current holdings and pending entries before the ten most recent completed positions, in pages of ten. Bought positions show venue/symbol, capture time (KST), actual fee-inclusive purchase cost in KRW, average execution unit price in native quote currency, fresh best bid and gross price return. Marks older than 15 seconds withhold current returns. Closed positions show fee/impact-inclusive realized return against actual purchase cost. Unfilled entries show only symbol, capture time and 매수 체결 대기 중. Pagination/refresh are read-only. This display/confirmation update preserves the reset marker, trading rules and all existing state.
