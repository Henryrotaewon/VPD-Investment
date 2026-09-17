"""Measurement confidence helpers for cross-venue lead/lag research.

The live system observes all venues on one Railway process.  We therefore use
received_ts_ms for ordering, and treat exchange timestamps as diagnostics only.
A lead is considered statistically resolvable only when it exceeds a
conservative pair-specific timing uncertainty derived from recent path jitter.
"""
from __future__ import annotations

MIN_PUBLIC_FEED_LEAD_MS = 250.0


def venue_jitter_ms(venue_diag: dict) -> float:
    return float(venue_diag.get('receive_jitter_ms') or 0.0)


def pair_min_resolvable_lead_ms(origin_diag: dict, follower_diag: dict) -> float:
    """Conservative public-feed resolution floor.

    Sum of the two venues' p95-p05 receive-vs-event timing spreads plus a
    hard 250 ms floor.  This is deliberately conservative: sub-second crypto
    price discovery exists, but public Internet WebSocket feeds should not be
    interpreted as colocated clocks.
    """
    return max(
        MIN_PUBLIC_FEED_LEAD_MS,
        venue_jitter_ms(origin_diag) + venue_jitter_ms(follower_diag),
    )


def classify_lead(lag_ms: float | None, origin_diag: dict,
                  follower_diag: dict) -> dict:
    threshold = pair_min_resolvable_lead_ms(origin_diag, follower_diag)
    if lag_ms is None:
        state = 'UNOBSERVABLE'
    elif abs(lag_ms) < threshold:
        state = 'UNRESOLVED_WITHIN_TIMING_NOISE'
    else:
        state = 'RESOLVED'
    return {
        'state': state,
        'lag_ms': lag_ms,
        'min_resolvable_lead_ms': round(threshold, 3),
        'clock_basis': 'same_process_received_ts_ms',
        'exchange_clock_role': 'diagnostic_only',
    }


def pair_resolution_matrix(feed_diagnostics: dict) -> dict:
    venues = sorted(feed_diagnostics)
    out = {}
    for origin in venues:
        for follower in venues:
            if origin == follower:
                continue
            a = feed_diagnostics[origin]
            b = feed_diagnostics[follower]
            out[f'{origin}->{follower}'] = {
                'min_resolvable_lead_ms': round(
                    pair_min_resolvable_lead_ms(a, b), 3),
                'measurement_ready': bool(
                    a.get('measurement_ready') and b.get('measurement_ready')),
            }
    return out
