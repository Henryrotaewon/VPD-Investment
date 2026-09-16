# MAGI1 Google Drive archival and lossless storage design

Date: 2026-09-16
Status: DESIGN ONLY. No Drive upload, credential provisioning, runtime change, or deletion is enabled by this document.

## Retention policy — user revision, 2026-09-16

This policy supersedes indefinite raw-event archival. It is a design, not an active
deletion job. Interpret "one week" as a rolling 7-day retention window, with
date/week folders for organization; upload continuously, not once a week.

| Dataset | Railway | Google Drive | Deletion rule |
|---|---|---|---|
| Normalized raw trades/books | Active segment and unverified upload queue only | Recent 7 days | Permanently delete expired segments ONLY after analysis completion and durable preservation of their derived outputs |
| Analysis/evidence | Most recent calendar month, plus unresolved work and necessary model state | Older analysis, retained indefinitely | Remove local rows only after verified remote archival; never age-delete analysis on Drive |
| Manifests, analysis versions and coverage gaps | Active catalog | Long-term evidence | Retain provenance and raw-deletion audit even after raw data is gone |

Use UTC event time for segment boundaries and age; apply one calendar month with
month-end clamping, not an undocumented 30-day approximation, for analysis migration.
Pending outcomes and the existing 30-day propagation lookback may cross the cutoff;
retain required state or restore it from Drive before trimming local records.

Analysis completion means: expected processing finished, all outcome horizons matured,
coverage/missing-data status recorded, features/labels/code versions preserved, output
row counts reconciled, and a verified backup of the derived evidence exists on Drive.
A low-confidence result or a missing-data outcome can be terminal; unresolved work cannot.
Recent analysis stays on Railway but receives incremental Drive backups before raw
deletion eligibility. Its primary archive moves to Drive after one month.

Permanent raw deletion targets only uploader-owned raw segment IDs in the dedicated
events folder, with age and manifest checks. Never target analysis, arbitrary account
files, or an entire mixed-content folder. A failed analysis retains its raw segment
beyond 7 days and raises backlog status. Do not delete earlier than 7 days by default.

After raw deletion, stored features and results remain inspectable, but arbitrary
future feature recomputation or full tick replay for that period is no longer possible.
Preserve non-triggered comparison cohorts, feed coverage, feature definitions,
thresholds, both timestamps, VPD provenance, and model/code versions as evidence.
The raw retention period does NOT change the Wave detection or outcome horizons.

At the measured raw rate, a 7-day rolling set is approximately 19.4 GB, plus temporary
overlap and failed-analysis backlog. Long-term analysis volume must be measured separately.
A provisional raw allocation of 25–30 GB is a planning allowance, not confirmed free quota.
A month of analysis is not guaranteed to fit 500 MB; measure DB growth and size-budget
diagnostics independently. Completed analysis remains recoverable when archived.

## Decision and preservation boundary

MAGI1 remains the canonical collector. Railway keeps a bounded working set; the user's
Google Drive stores raw segments until policy expiry and long-term analysis archives. VPD and MAGI2 remain independent.

During the raw retention window, preserve every currently collected normalized event, including order, receive/exchange
timestamps, venue, symbol, price, quantity, side, trade ID, sequence and available top-10
book levels. Existing normalized data is NOT the original exchange wire payload:
normalization already uses floats and truncates depth. A new archive cannot recover
previously discarded precision, deeper levels or unsaved raw messages. Lossless below
means round-trip equality relative to the stored normalized records, not wire reconstruction.

Never replace event history with candles, 1-second sampling, or event-only capture.
Aggregates and selected shock windows are additional derived datasets. No blanket
timestamp-based deduplication of trades or books.

## Observed baseline and capacity

2026-09-16 11:06:43–11:19:44 KST, 13.007 minutes:
- free space decreased 29,605,888 bytes: 136.56 MB/hour across the volume;
- compressed JSONL raw grew 25,069,492 bytes: 115.64 MB/hour, 2.775 GB/day;
- latest free bytes: 400,846,848; current runtime guard: 100 MiB;
- total-growth extrapolation: 3.278 GB/day; SQLite/WAL growth is not necessarily linear.

User reports a 100 GB plan; actual remaining quota is not verified. Do not allocate all
100 GB: existing Drive/Gmail/Photos use and other files must be accounted for.
Raw data now expires under the 7-day, analysis-verified rule above; the earlier
18–22-day estimate for a 60 GB indefinitely growing raw archive no longer describes
this policy. Analysis remains cumulative and will eventually require more capacity.
Store measured raw/analysis bytes per day and remaining quota separately.

## Storage layout

| Location | Contents | Policy |
|---|---|---|
| Railway /data/magi1/spool | Active and sealed event segments | Seal every 5 minutes or 16 MiB, whichever comes first |
| Railway research.db | Active research records, checkpoints, upload catalog | Recent calendar month plus unresolved work; verified migration of older resolved rows |
| Drive MAGI1-Archive/events/YYYY/MM/DD | Immutable event archives, initially .jsonl.gz | Every sealed segment uploaded and verified; 7-day expiry after completed analysis |
| Drive MAGI1-Archive/research/YYYY/MM/DD | Resolved research rows and coherent SQLite snapshots | Preserve full records, IDs, schemas and provenance |
| Drive MAGI1-Archive/manifests/YYYY/MM/DD | Segment manifests and coverage reports | Uploaded with each archive batch |
| Drive MAGI1-Archive/reports | Daily human-readable reports | Derived; not a replacement for events |

Suggested Railway working-set budget (initial, not guaranteed):
150 MB spool, 120 MB active DB/WAL, 60 MB conversion/upload workspace, and 100 MiB
free reserve. Filesystem overhead and other files count against measured free space.
Spool targets yield roughly an hour of outage tolerance at the observed raw rate,
less when traffic rises. Do not implement a time-only promise such as "always 2 hours".
500 MB is a working-set constraint, not a sustainable standalone archive.

## Archival protocol

State machine:
OPEN -> SEALED -> UPLOADING -> REMOTE_VERIFIED -> LOCAL_EVICTABLE -> LOCAL_REMOVED.

1. Finish a segment, flush/fsync and atomically seal it. Never upload a still-growing file.
2. Record a unique segment ID, schema version, collector commit/session, venue/assets,
   event count, time range, receive-order ordinal, bytes and SHA-256 in a manifest.
3. Use Drive resumable upload. Persist session/progress privately to survive restarts.
4. Bind a stable remote file ID and segment ID; reconcile after timeout before creating
   duplicates. Names are not unique in Drive. Never overwrite archives in place.
5. Verify remote size and checksum against local bytes. Prefer server-reported checksum
   when available; otherwise download and hash before deletion. Merely storing a local
   SHA-256 in remote custom metadata does not independently verify the remote bytes.
6. Persist the verified file ID, checksum and remote manifest in a committed catalog.
7. Only then may the local copy be removed. Recovery must be idempotent across every
   transition; local loss after verification must still be discoverable from Drive.
8. Perform periodic restore/parse/row-count checks and log their outcomes.

Resumable upload, rate-limit backoff and retry are required. OAuth/authentication
failure, quota exhaustion or checksum mismatch must leave the local segment intact.
At low free space with unverified data, pause collection and record a coverage gap.
It is impossible to guarantee both uninterrupted collection and zero loss through an
arbitrarily long outage with finite local disk. Never overwrite the backlog to hide this.

Replace the current age-only raw deletion rule with verification-gated eviction before
enabling production archival. Existing files older than two days must not be deleted
solely because of age. Do not change the live retention policy until this is implemented.

## Lossless reduction, in implementation order

### 1. Safer batching using the existing format

Initially archive the existing normalized JSONL.gz records unchanged. Coalesce sealed
segments when practical instead of keeping thousands of tiny files. Avoid aggressive
gzip compression in the collection critical path; benchmark CPU and queue lag.

### 2. Parquet with Zstandard (benchmark-gated)

Use typed columns, dictionary encoding for venue/symbol/side, integer timestamps and
IDs as strings where necessary. Preserve existing float64 values exactly; do not
downcast to float32 or round prices. Preserve null versus zero, sequence and event order.
Store book levels with ordered level indexes; partition without creating one tiny file
per venue/asset every few seconds.

Test source -> Parquet -> decoded records for field equality, row count, ordering,
timestamp bounds and canonical content hash. JSON byte layout need not be identical.
Delete source only after round-trip verification AND remote verification of replacement.
Benchmark against the CURRENT gzip files, not uncompressed JSON. Compression improvement
is unknown; retain gzip if Parquet is larger or materially worsens collection latency.

### 3. Book snapshot plus lossless changes

After Parquet correctness is established, encode the existing top-10 book state as
a full snapshot at each segment start plus state changes. Keep every event timestamp
and receive ordinal even when state is unchanged (references/run-length metadata).
Keep deletions, side, price, size, sequence and reconnect boundaries; force a new
snapshot after gaps. Each segment must decode independently.

Compare reconstructed state after EVERY event with source books. This preserves
observed states only; it does not invent intra-update exchange activity.
All trades remain preserved. Repeated price/size trades are not redundant.

### 4. Bound the research DB without losing history

Use stable record IDs and archive completed records in sealed immutable batches,
with remote verification before local deletion. Do not purge unresolved episodes,
pending labels, restart checkpoints or required lookback state.
Preserve at least the 30-day propagation lookback used by ResearchEngine, or implement
an exact replacement state before purging those rows. VPD point-in-time inputs and
source commits must remain available for replay.

Use SQLite's backup API for consistent snapshots (copying research.db alone while WAL
is active is insufficient). Archive batches incrementally; do not repeatedly upload a
full ever-growing DB. Checkpoint WAL; VACUUM needs temporary space and must not run
blindly on the small production volume. Measure real bytes reclaimed.

## Drive authentication

The ChatGPT Google Drive connection supports interactive inspection; it does not
automatically supply credentials to the Railway worker. The unattended uploader needs
a separate user-authorized Google OAuth client with offline access and a refresh token.
Use the user's own Drive identity, preferably drive.file scope for files created by
this uploader. Create a dedicated archive folder through that identity.
Refresh token/client secret belong only in Railway secrets, never GitHub, logs or chat.
Verify token lifecycle and avoid a short-lived testing consent configuration.

A service account by itself cannot consume a personal My Drive quota as file owner.
Workspace shared-drive alternatives are outside this personal-account design.

Before enabling: confirm remaining quota, create the dedicated folder, upload a small
test archive, retrieve and verify it, then verify refresh-token renewal from Railway.
Connection to ChatGPT alone is not completion of these gates.

## Acceptance checks and rollout

1. Add archival queue, segment sealing and manifest tests while keeping upload disabled.
2. User authorizes the uploader; upload and restore a test file without touching existing files.
3. Archive existing closed segments; reconcile checksums and catalog.
4. Enable verification-gated local eviction. Simulate quota failure, upload interruption,
   process restart, remote corruption and disk pressure.
5. Benchmark gzip versus Parquet using actual quiet/busy market segments.
6. Enable a new encoding only after exact replay equality; keep schema-versioned readers.
7. Report queue bytes/oldest age, last verified upload, quota remaining, bytes/day,
   projected days, missing intervals and DB/WAL sizes. New alerts require an explicitly
   chosen notification destination; no messages are sent by this design.

## Sources

- Drive resumable uploads: https://developers.google.com/workspace/drive/api/guides/manage-uploads
- User OAuth/offline access: https://developers.google.com/identity/protocols/oauth2/web-server
- Service-account ownership limits: https://developers.google.com/workspace/drive/api/guides/about-shareddrives
- Parquet compression and dictionary encoding: https://arrow.apache.org/docs/python/parquet.html
