"""Empirical receive frequencies with Wilson intervals and explicit sample counts."""
import math
from collections import defaultdict
from statistics import median

def wilson(success,n):
    if not n: return [None,None]
    z=1.96; p=success/n; d=1+z*z/n
    center=(p+z*z/(2*n))/d; half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [max(0,center-half),min(1,center+half)]

class PropagationStats:
    def __init__(self): self.samples=defaultdict(list)
    def add(self,row):
        key=tuple(row[x] for x in ('asset','direction','horizon','origin_venue','follower_venue'))
        self.samples[key].append(row)
        self.samples[key]=self.samples[key][-10000:]
        xs=self.samples[key]; successes=sum(x['received'] for x in xs); lags=[x['lag_ms'] for x in xs if x['lag_ms'] is not None]
        return {**row,'sample_count':len(xs),'reception_probability':successes/len(xs),'reception_probability_ci95':wilson(successes,len(xs)),'lag_median_ms':median(lags) if lags else None,'reliable':len(xs)>=30,'origin_confidence':None,'confidence_status':'origin causality not calibrated; local reception timing only'}
