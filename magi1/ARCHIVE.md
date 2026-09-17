# Verified Drive archival

Set `MAGI1_DRIVE_ARCHIVE_ENABLED=1` with the existing three `MAGI1_GOOGLE_*`
OAuth variables. The collector remains COLLECT_ONLY. A background task runs
at startup and every five minutes, using a private app-owned MAGI1-Archive
folder. Credentials are never printed.

Closed hourly gzip files are uploaded after a one-hour grace period and five
minutes without modification. The remote MD5 and size must match before the
local copy is removed. Upload/verification failures preserve the local source.
The 100 MiB free-space stop remains enabled. Raw files are no longer deleted
merely because their local retention age elapsed.

Research SQLite snapshots are transactionally backed up, compressed, and
checksum verified on Drive before removing database records older than 30 days.
Snapshots include checkpoints and remain on Drive permanently. They are full
snapshots, created when a new raw batch is archived and at least daily; monitor
Drive usage as analysis grows. SQLite freed pages are reused by future inserts.

Drive raw objects older than seven days are permanently deleted ONLY if they
have uninterrupted whole-hour analysis coverage recorded by this process and
their corresponding research snapshot still exists. Historical raw objects or
hours interrupted by restart are retained on Drive because their coverage
cannot be proven. These exceptions require verified replay before deletion.

Logs: archive_research_verified, archive_raw_verified, archive_cycle_ok,
archive_cycle_failed. A failed OAuth refresh requires renewing authentication;
the service does not print tokens or discard unarchived local data.
