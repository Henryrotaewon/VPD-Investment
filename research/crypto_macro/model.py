"""Point-in-time feature joins and purged walk-forward ridge forecasts.

Inputs are audited, precomputed feature observations and separate price outcomes.
This module neither downloads unversioned history nor enables a trading strategy.
"""
from collections import defaultdict
from dataclasses import dataclass
import math
import numpy as np

DAY = 86_400_000
VERSION = 'crypto-macro-research-v0.1'


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('NONFINITE_VALUE')
    return value


def timestamp(value):
    if isinstance(value, bool) or int(value) != value or value < 0:
        raise ValueError('INVALID_UTC_MILLISECONDS')
    return int(value)


class AsOfFeatures:
    def __init__(self, observations, specs):
        self.specs = specs
        self.rows = defaultdict(list)
        if not specs or len({s['id'] for s in specs}) != len(specs):
            raise ValueError('INVALID_FEATURE_MANIFEST')
        if any(finite(s['max_age_days']) <= 0 for s in specs):
            raise ValueError('INVALID_STALENESS_LIMIT')
        self.names = [s['id'] for s in specs]
        seen = set()
        for row in observations:
            if row['series'] not in self.names:
                raise ValueError('UNKNOWN_FEATURE')
            if row['quality'] not in ('RECORDED_ASOF', 'VERIFIED_VINTAGE') or not row['source']:
                raise ValueError('UNVERIFIED_HISTORY')
            observed = timestamp(row['observed_ms']); available = timestamp(row['available_ms'])
            if available < observed:
                raise ValueError('AVAILABILITY_BEFORE_OBSERVATION')
            ident = (row['series'], observed, available)
            if ident in seen:
                raise ValueError('DUPLICATE_VINTAGE')
            seen.add(ident)
            self.rows[row['series']].append(dict(row, value=finite(row['value'])))
        for rows in self.rows.values():
            rows.sort(key=lambda x:(x['observed_ms'],x['available_ms']))

    def at(self, cutoff):
        values = []; provenance = []; missing = []
        for spec in self.specs:
            eligible = [r for r in self.rows[spec['id']]
                        if r['observed_ms'] <= cutoff and r['available_ms'] <= cutoff]
            row = eligible[-1] if eligible else None
            if row is None or cutoff-row['observed_ms'] > spec['max_age_days']*DAY:
                missing.append(spec['id']); continue
            values.append(row['value'])
            provenance.append(dict(series=spec['id'], observed_ms=row['observed_ms'],
                                   available_ms=row['available_ms'], source=row['source']))
        # Never replace absent economic releases with neutral zeros.
        return (None, missing, provenance) if missing else (np.array(values), [], provenance)


def outcomes(rows):
    result = []; seen = set()
    for row in rows:
        t = dict(row)
        if t['asset'] not in ('BTC','ETH') or t['horizon_days'] not in (1,7):
            raise ValueError('UNKNOWN_TARGET')
        if t['quote'] != 'USD':
            raise ValueError('USD_TARGET_REQUIRED')
        for key in ('decision_ms','end_ms','available_ms'):
            t[key] = timestamp(t[key])
        if t['end_ms'] != t['decision_ms']+t['horizon_days']*DAY or t['available_ms'] < t['end_ms']:
            raise ValueError('INVALID_OUTCOME_TIMING')
        start = finite(t['start_price']); end = finite(t['end_price'])
        if start <= 0 or end <= 0 or not t['source']:
            raise ValueError('INVALID_OUTCOME_PRICE')
        key = (t['asset'],t['horizon_days'],t['decision_ms'])
        if key in seen:
            raise ValueError('DUPLICATE_OUTCOME')
        seen.add(key)
        t['y'] = math.log(end/start)
        result.append(t)
    return sorted(result,key=lambda r:r['decision_ms'])


@dataclass(frozen=True)
class Policy:
    min_train: int = 365
    validation: int = 60
    window_days: int = 1095
    refit_days: int = 30
    alphas: tuple = (1.0, 10.0, 100.0)

    def __post_init__(self):
        if self.min_train < 2 or self.validation < 2 or self.window_days < 1 or self.refit_days < 1:
            raise ValueError('INVALID_RESEARCH_POLICY')
        if not self.alphas or any(not math.isfinite(a) or a <= 0 for a in self.alphas):
            raise ValueError('INVALID_RIDGE_PENALTY')


def fit(x, y, alpha):
    center = x.mean(axis=0); scale = x.std(axis=0)
    scale[scale < 1e-10] = 1.0
    z = (x-center)/scale
    intercept = float(y.mean())
    beta = np.linalg.solve(z.T@z+alpha*np.eye(x.shape[1]),z.T@(y-intercept))
    return dict(center=center, scale=scale, beta=beta, intercept=intercept,
                target_scale=max(float(y.std()),1e-8))


def predict(model, x):
    contribution = (x-model['center'])/model['scale']*model['beta']
    return model['intercept']+contribution.sum(axis=-1)


def select_model(rows, columns, policy):
    """Choose alpha using one chronological inner holdout, purging overlapping labels."""
    if len(rows) < policy.min_train+policy.validation:
        return None
    validation = rows[-policy.validation:]
    first = validation[0]['decision_ms']
    training = [r for r in rows[:-policy.validation]
                if r['end_ms'] < first and r['available_ms'] < first]
    if len(training) < policy.min_train:
        return None
    x = np.array([r['x'][columns] for r in training]); y = np.array([r['y'] for r in training])
    vx = np.array([r['x'][columns] for r in validation]); vy = np.array([r['y'] for r in validation])
    scores = [(float(np.mean((predict(fit(x,y,a),vx)-vy)**2)),a) for a in policy.alphas]
    alpha = min(scores)[1]
    final = fit(np.array([r['x'][columns] for r in rows]),np.array([r['y'] for r in rows]),alpha)
    final.update(alpha=alpha, inner_train_last_end_ms=max(r['end_ms'] for r in training),
                 inner_validation_start_ms=first, training_rows=len(rows))
    return final


def run(observations, target_rows, specs, asof, policy=Policy()):
    asof = timestamp(asof)
    features = AsOfFeatures(observations,specs)
    target_rows = outcomes(target_rows)
    groups = {'crypto_only':[i for i,s in enumerate(specs) if s['family']=='crypto'],
              'macro_only':[i for i,s in enumerate(specs) if s['family']=='macro'],
              'mixed':list(range(len(specs)))}
    if not groups['crypto_only'] or not groups['macro_only']:
        raise ValueError('BOTH_BASELINE_FAMILIES_REQUIRED')
    cache = {}
    for t in target_rows:
        if t['decision_ms'] <= asof and t['decision_ms'] not in cache:
            cache[t['decision_ms']] = features.at(t['decision_ms'])
    cache[asof] = features.at(asof)
    forecasts = []; gaps = []; coverage = defaultdict(int)
    for asset in ('BTC','ETH'):
        for horizon in (1,7):
            rows = [dict(t,x=cache[t['decision_ms']][0]) for t in target_rows
                    if t['asset']==asset and t['horizon_days']==horizon and t['decision_ms']<=asof]
            by_time = {r['decision_ms']:r for r in rows}
            origins = sorted(set(by_time)|{asof})
            fitted = None; fit_time = None
            for ts in origins:
                x, missing, provenance = cache[ts]
                if missing:
                    for name in missing: coverage[name]+=1
                    gaps.append(dict(asset=asset,horizon_days=horizon,decision_ms=ts,reason='MISSING_OR_STALE_FEATURES',features=missing))
                    continue
                if fitted is None or ts-fit_time >= policy.refit_days*DAY:
                    train = [r for r in rows if r['x'] is not None and
                             ts-policy.window_days*DAY <= r['decision_ms'] < ts and
                             r['end_ms'] < ts and r['available_ms'] <= ts]
                    candidates = {name:select_model(train,cols,policy) for name,cols in groups.items()}
                    if any(m is None for m in candidates.values()):
                        gaps.append(dict(asset=asset,horizon_days=horizon,decision_ms=ts,reason='INSUFFICIENT_TRAINING'))
                        continue
                    fitted = candidates; fit_time = ts
                    train_max_available = max(r['available_ms'] for r in train)
                    train_max_end = max(r['end_ms'] for r in train)
                estimates = {name:float(predict(m,x[groups[name]])) for name,m in fitted.items()}
                full = fitted['mixed']
                z = (x-full['center'])/full['scale']
                actual = by_time.get(ts)
                observed = actual is not None and actual['available_ms'] <= asof
                forecasts.append(dict(asset=asset,horizon_days=horizon,decision_ms=ts,
                    end_ms=ts+horizon*DAY, fit_ms=fit_time,train_max_available_ms=train_max_available,
                    train_max_end_ms=train_max_end,training_rows=full['training_rows'],
                    selected_alphas={k:m['alpha'] for k,m in fitted.items()}, predictions=estimates,
                    historical_mean=full['intercept'], actual_log_return=actual['y'] if observed else None,
                    start_price=actual['start_price'] if observed else None,
                    end_price=actual['end_price'] if observed else None,
                    pressure_index=50+50*math.tanh(estimates['mixed']/full['target_scale']),
                    standardized_weights=dict(zip(features.names,full['beta'].tolist())),
                    contributions=dict(zip(features.names,(z*full['beta']).tolist())),
                    available_inputs=provenance))
    return dict(schema=VERSION,mode='RESEARCH_ONLY',asof_ms=asof,
                status='UNVALIDATED_FORECASTS' if forecasts else 'DATA_OR_HISTORY_REQUIRED',
                live_enabled=False,pressure_index_is_probability=False,
                forecasts=forecasts,gaps=gaps,missing_counts=dict(coverage),
                metrics=evaluate(forecasts),promotion='NOT_EVALUATED')


def evaluate(forecasts):
    output = []
    for asset in ('BTC','ETH'):
        for horizon in (1,7):
            rows = [r for r in forecasts if r['asset']==asset and r['horizon_days']==horizon and r['actual_log_return'] is not None]
            if not rows:
                continue
            y = np.array([r['actual_log_return'] for r in rows])
            mean = np.array([r['historical_mean'] for r in rows])
            reference = float(np.sum((y-mean)**2))
            models = {}
            for name in ('crypto_only','macro_only','mixed'):
                p = np.array([r['predictions'][name] for r in rows]); mse = float(np.mean((y-p)**2))
                corr = float(np.corrcoef(y,p)[0,1]) if len(y)>2 and y.std()>1e-10 and p.std()>1e-10 else None
                models[name] = dict(mse=mse,r2_vs_historical_mean=1-float(np.sum((y-p)**2))/reference if reference>1e-15 else None,
                    return_correlation=corr,direction_accuracy=float(np.mean(np.sign(y)==np.sign(p))))
            improvement = models['crypto_only']['mse']-models['mixed']['mse']
            output.append(dict(asset=asset,horizon_days=horizon,n=len(rows),models=models,
                               zero_change_mse=float(np.mean(y*y)),
                               mixed_mse_improvement_vs_crypto=improvement,
                               inference='DESCRIPTIVE_ONLY_BLOCK_BOOTSTRAP_AND_FINAL_HOLDOUT_REQUIRED'))
    return output
