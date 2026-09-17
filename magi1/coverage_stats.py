"""Observed reception interval and missing-outcome identification bounds.

Unknown address ownership is a separate stratum, never a failed reception.
Wilson intervals assume independent trials; repeated/overlapping shocks must
be deduplicated or analysed with cluster resampling before predictive claims.
"""
from .propagation import wilson


def reception_summary(received, non_reaction, unobservable):
    counts = (received, non_reaction, unobservable)
    if any(type(x) is not int or x < 0 for x in counts):
        raise ValueError('counts must be nonnegative integers')
    observed = received + non_reaction
    total = observed + unobservable
    return {
        'received': received, 'non_reaction': non_reaction,
        'unobservable': unobservable, 'total': total,
        'observed_probability': received / observed if observed else None,
        'observed_wilson_ci95': wilson(received, observed),
        'coverage': observed / total if total else None,
        'missing_outcome_bounds': [received / total,
                                   (received + unobservable) / total] if total else [None, None],
        'bounds_are_confidence_interval': False,
        'interval_assumption': 'independent trials; not causal or prediction confidence',
    }
