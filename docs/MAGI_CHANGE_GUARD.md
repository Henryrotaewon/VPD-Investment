# MAGI change guard

MAGI1 is the protected market-intelligence source.

For MAGI3 work:
- Do not change MAGI1 raw trade/book formats, FeatureEngine windows, formation/propagation semantics, storage names, evaluation cohorts, timing-quality logic, or retention behavior.
- Do not add exchange credentials or order clients to MAGI1.
- Consume MAGI1 through read-only adapters/contracts.
- Any future MAGI1 Trend/Rotation/Breadth/Exhaustion addition must be additive: new derived records/state, never a mutation of historical Wave records.
- Before merging a change that touches magi1/, run the existing MAGI1 test suite and an explicit schema/regression review.
- MAGI3 development should normally have zero diff under magi1/.

Protected baseline for this branch: main commit fc8647e4bfa966e12e4bafdc7290d9f88d4c05f9.
