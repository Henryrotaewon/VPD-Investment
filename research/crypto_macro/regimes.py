"""Four causal market regimes and costed, unlevered allocation experiments.

Daily reference prices are an exploratory historical snapshot, not executable
quotes or verified point-in-time macro history. Nothing here places orders.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import hashlib
import math
import numpy as np
from .model import DAY, finite, timestamp

REGIMES = ('UP_LOW', 'UP_HIGH', 'DOWN_LOW', 'DOWN_HIGH')
CANDIDATES = ('cash', 'equal', 'momentum', 'inverse_vol')
CAPS = dict(zip(REGIMES, (.8, .4, .2, 0.)))


def date_ms(value):
    return int(datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000)


@dataclass(frozen=True)
class RegimePolicy:
    trend_days: int = 90
    vol_days: int = 30
    vol_reference_days: int = 252
    vol_quantile: float = .65
    confirmation_days: int = 2
    signal_lag_days: int = 2
    selection_window_days: int = 365
    minimum_regime_samples: int = 30
    selection_refit_days: int = 30

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == 'vol_quantile':
                if not 0 < value < 1: raise ValueError('INVALID_QUANTILE')
            elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError('INVALID_REGIME_POLICY')
        if self.vol_days < 2 or self.signal_lag_days < 2:
            raise ValueError('TWO_DAY_REFERENCE_PRICE_LAG_REQUIRED')


def read_coinmetrics(btc_path, eth_path, start, asof):
    """Keep PriceUSD distinct from ReferenceRateUSD; never silently substitute."""
    series = []; sources = []
    for asset, path in zip(('BTC', 'ETH'), (btc_path, eth_path)):
        path = Path(path); data = {}
        with path.open() as stream:
            for row in csv.DictReader(stream):
                ts = date_ms(row['time'])
                if ts < start or ts > asof-DAY or not row.get('PriceUSD'): continue
                if ts in data: raise ValueError('DUPLICATE_PRICE_DATE')
                price = finite(row['PriceUSD'])
                if price <= 0: raise ValueError('NONPOSITIVE_PRICE')
                data[ts] = price
        series.append(data)
        sources.append(dict(asset=asset, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            field='PriceUSD', source='https://github.com/coinmetrics/data'))
    times = sorted(set(series[0]) & set(series[1]))
    if not times: raise ValueError('NO_COMMON_PRICE_HISTORY')
    if any(b-a != DAY for a,b in zip(times,times[1:])):
        raise ValueError('MISSING_DAILY_PRICES_NO_FORWARD_FILL')
    return [dict(ts_ms=t, prices=[x[t] for x in series]) for t in times], sources


def validate_panel(panel):
    times = np.array([timestamp(r['ts_ms']) for r in panel], dtype=np.int64)
    prices = np.array([r['prices'] for r in panel], dtype=float)
    if len(times) < 2 or prices.shape != (len(times), 2): raise ValueError('BTC_ETH_PANEL_REQUIRED')
    if np.any(np.diff(times) != DAY): raise ValueError('DAILY_CONTIGUOUS_PANEL_REQUIRED')
    if not np.all(np.isfinite(prices)) or np.any(prices <= 0): raise ValueError('INVALID_PRICE')
    return times, prices


def macro_directions(report):
    """Consume issued seven-day predictions only; ignore every realized outcome."""
    if report.get('mode') != 'RESEARCH_ONLY': raise ValueError('RESEARCH_FORECAST_REPORT_REQUIRED')
    rows = {}
    for row in report['forecasts']:
        if row['horizon_days'] != 7: continue
        ts = timestamp(row['decision_ms']); fit = timestamp(row['fit_ms'])
        if (row['asset'] not in ('BTC','ETH') or fit > ts or
            row['train_max_end_ms'] >= fit or row['train_max_available_ms'] > fit or
            any(x['available_ms'] > ts or x['observed_ms'] > ts for x in row['available_inputs'])):
            raise ValueError('NONCAUSAL_MACRO_FORECAST')
        key = (ts,row['asset'])
        if key in rows: raise ValueError('DUPLICATE_MACRO_FORECAST')
        rows[key] = finite(row['predictions']['mixed'])
    return rows


def classify(panel, policy=RegimePolicy(), macro_report=None):
    times, prices = validate_panel(panel); n = len(times)
    log_returns = np.diff(np.log(prices), axis=0)
    vols = np.full(n, np.nan); asset_vols = np.full((n,2), np.nan)
    for j in range(policy.vol_days,n):
        sample = log_returns[j-policy.vol_days:j]
        vols[j] = np.std(sample.mean(axis=1),ddof=1)*math.sqrt(365)
        asset_vols[j] = np.std(sample,axis=0,ddof=1)*math.sqrt(365)
    forecasts = macro_directions(macro_report) if macro_report is not None else None
    state = None; pending = None; streak = 0; signals = []
    for i,ts in enumerate(times):
        j = i-policy.signal_lag_days
        if j < max(policy.trend_days,policy.vol_days+policy.vol_reference_days):
            signals.append(None); continue
        reference = vols[j-policy.vol_reference_days:j]
        threshold = float(np.quantile(reference,policy.vol_quantile))
        trend = np.log(prices[j]/prices[j-policy.trend_days])
        direction = trend
        if forecasts is not None:
            keys = [(int(times[j]),a) for a in ('BTC','ETH')]
            if not all(k in forecasts for k in keys):
                signals.append(None); state=None; pending=None; streak=0; continue
            direction = np.array([forecasts[k] for k in keys])
        raw = ('UP' if float(direction.mean()) > 0 else 'DOWN') + ('_HIGH' if vols[j] > threshold else '_LOW')
        streak = streak+1 if raw == pending else 1; pending = raw
        if streak >= policy.confirmation_days: state = raw
        if state is None:
            signals.append(None); continue
        # Reduce risk immediately during an adverse transition; increase only
        # after the required consecutive confirmations. No fabricated confidence.
        cap = min(CAPS[state],CAPS[raw])
        signals.append(dict(decision_ms=int(ts), input_cutoff_ms=int(times[j]),
            regime=state, raw_regime=raw, transition=state!=raw, risk_cap=cap,
            direction_basis='MIXED_7D_FORECAST' if forecasts is not None else 'TRAILING_90D_PRICE',
            direction=direction.tolist(), volatility=float(vols[j]),
            volatility_threshold=threshold, asset_volatility=asset_vols[j].tolist(),
            disagreement=bool(direction[0]*direction[1]<0)))
    return signals


def weights(strategy, signal):
    cap = signal['risk_cap']
    if strategy == 'cash': return np.zeros(2)
    if strategy == 'equal': return np.full(2,cap/2)
    if strategy == 'momentum':
        eligible = np.maximum(signal['direction'],0)
        return cap*eligible/eligible.sum() if eligible.sum()>0 else np.zeros(2)
    if strategy == 'inverse_vol':
        inv = 1/np.maximum(signal['asset_volatility'],1e-6)
        return cap*inv/inv.sum()
    raise ValueError('UNKNOWN_STRATEGY')


def rebalance(holdings, target, cost):
    """Self-financing target weights AFTER proportional transaction costs.

    holdings = [BTC value, ETH value, cash], all in one USD numeraire.
    Cost is applied to absolute risky-asset notional traded, on buys AND sells.
    """
    holdings=np.asarray(holdings,dtype=float); target=np.asarray(target,dtype=float)
    if (holdings.shape != (3,) or target.shape != (2,) or
        not np.all(np.isfinite(holdings)) or not np.all(np.isfinite(target)) or
        np.any(holdings < -1e-12) or np.any(target < 0) or target.sum()>1+1e-12 or
        not 0 <= cost < .1): raise ValueError('INVALID_UNLEVERED_PORTFOLIO')
    nav=float(holdings.sum())
    if nav <= 0: raise ValueError('NONPOSITIVE_NAV')
    # Bisection solves after = before - c*sum(abs(target*after - old)).
    low=0.; high=nav
    for _ in range(45):
        mid=(low+high)/2
        if mid+cost*np.abs(target*mid-holdings[:2]).sum()>nav: high=mid
        else: low=mid
    after=(low+high)/2; risky=target*after
    turnover=float(np.abs(risky-holdings[:2]).sum())
    fee=cost*turnover
    result=np.r_[risky,nav-fee-risky.sum()]
    if result[2] < -1e-9*nav: raise ValueError('NEGATIVE_CASH')
    return result,fee,turnover


def portfolio_step(holdings, target, asset_returns, cost):
    before=float(np.sum(holdings))
    funded,fee,turnover=rebalance(holdings,target,cost)
    funded[:2] *= 1+np.asarray(asset_returns)
    if not np.all(np.isfinite(funded)) or np.any(funded < 0): raise ValueError('INVALID_MARK')
    return funded,dict(net_return=float(funded.sum()/before-1),
        cost_fraction=fee/before,turnover=turnover/before,exposure=float(np.sum(target)))


def select_strategy(history, ts, regime, policy):
    rows=[r for r in history if ts-policy.selection_window_days*DAY<=r['decision_ms'] and
          r['end_ms']<ts and r['regime']==regime]
    if len(rows)<policy.minimum_regime_samples:
        return dict(strategy='cash',n=len(rows),reason='INSUFFICIENT_REGIME_HISTORY',
                    fitted_ms=ts,last_outcome_ms=max((r['end_ms'] for r in rows),default=None))
    scores={s:float(np.mean([math.log1p(r['candidates'][s]['net_return']) for r in rows])) for s in CANDIDATES}
    # cash is a real zero-yield candidate and wins ties; no forced investment.
    chosen=max(CANDIDATES,key=lambda s:scores[s])
    return dict(strategy=chosen,n=len(rows),reason='PAST_NET_LOG_GROWTH',scores=scores,
                fitted_ms=ts,last_outcome_ms=max(r['end_ms'] for r in rows))


def simulate(panel, signals, start, cost_bps=15, policy=RegimePolicy()):
    times,prices=validate_panel(panel)
    cost=finite(cost_bps)/10000
    accounts={s:np.array([0.,0.,1.]) for s in CANDIDATES}
    active={s:np.array([0.,0.,1.]) for s in ('adaptive','regime_equal','fixed_50','buy_hold_100','cash')}
    history=[]; daily=[]; selection={}; began=False
    for i in range(len(times)-1):
        ts=int(times[i]); signal=signals[i]
        if signal is None:
            if ts>=start: raise ValueError('MISSING_REGIME_IN_EVALUATION')
            continue
        asset_returns=prices[i+1]/prices[i]-1
        current={}
        for name in CANDIDATES:
            accounts[name],current[name]=portfolio_step(accounts[name],weights(name,signal),asset_returns,cost)
        regime=signal['regime']
        old=selection.get(regime)
        if old is None or ts-old['fitted_ms']>=policy.selection_refit_days*DAY:
            selection[regime]=select_strategy(history,ts,regime,policy)
        chosen=selection[regime]
        if ts>=start:
            targets={'adaptive':weights(chosen['strategy'],signal),
                     'regime_equal':weights('equal',signal),'fixed_50':np.array([.25,.25]),
                     'cash':np.zeros(2)}
            hold=active['buy_hold_100']
            targets['buy_hold_100']=hold[:2]/hold.sum() if began else np.array([.5,.5])
            paths={}
            for name in active:
                active[name],paths[name]=portfolio_step(active[name],targets[name],asset_returns,cost)
                paths[name]['nav']=float(active[name].sum())
            daily.append(dict(**signal,end_ms=int(times[i+1]),selection=dict(chosen),
                target_weights=dict(BTC=float(targets['adaptive'][0]),ETH=float(targets['adaptive'][1]),
                                    CASH=float(1-targets['adaptive'].sum())),portfolios=paths))
            began=True
        # Append only after this period's decision. Today's result cannot select
        # today's strategy, even when the entire price panel is loaded in memory.
        history.append(dict(decision_ms=ts,end_ms=int(times[i+1]),regime=regime,candidates=current))
    return daily


def metrics(rows, name):
    if not rows: return None
    r=np.array([x['portfolios'][name]['net_return'] for x in rows])
    wealth=np.r_[1.,np.cumprod(1+r)]
    drawdown=wealth/np.maximum.accumulate(wealth)-1
    sd=float(np.std(r,ddof=1)) if len(r)>1 else 0.
    return dict(days=len(r),total_return=float(wealth[-1]-1),
        cagr=float(wealth[-1]**(365/len(r))-1) if len(r)>=365 else None,
        max_drawdown=float(drawdown.min()),annualized_volatility=sd*math.sqrt(365),
        sharpe_zero_cash=float(np.mean(r)/sd*math.sqrt(365)) if sd>1e-12 else None,
        mean_exposure=float(np.mean([x['portfolios'][name]['exposure'] for x in rows])),
        turnover_sum=float(sum(x['portfolios'][name]['turnover'] for x in rows)))


def block_comparison(rows, samples=2000, block=30, seed=741):
    if len(rows)<2*block: return dict(status='INSUFFICIENT_BLOCKS')
    diff=np.array([math.log1p(r['portfolios']['adaptive']['net_return'])-
                   math.log1p(r['portfolios']['fixed_50']['net_return']) for r in rows])
    rng=np.random.default_rng(seed); n=len(diff); means=[]
    for _ in range(samples):
        starts=rng.integers(0,n-block+1,size=math.ceil(n/block))
        sample=np.concatenate([diff[s:s+block] for s in starts])[:n]
        means.append(float(sample.mean()))
    means=np.array(means); observed=float(diff.mean())
    # Center the null bootstrap; p is one-sided for positive excess log growth.
    p=float((1+np.sum(means-observed>=observed))/(samples+1))
    return dict(status='EXPLORATORY_DEPENDENT_BLOCK_BOOTSTRAP',block_days=block,samples=samples,
        annualized_log_growth_difference=observed*365,
        annualized_log_growth_ci95=(np.quantile(means,[.025,.975])*365).tolist(),
        one_sided_p=p,warning='Historical snapshot, selected universe; not evidence for live promotion')


def run_regimes(panel, asof, *, evaluation_start, holdout_start, policy=RegimePolicy(),
                costs=(15,30), macro_report=None, bootstrap_samples=2000):
    asof=timestamp(asof)
    # Drop future rows before feature computation or evaluation.
    panel=[r for r in panel if timestamp(r['ts_ms'])<=asof-DAY]
    times,_=validate_panel(panel)
    if not evaluation_start<holdout_start<=int(times[-1]): raise ValueError('INVALID_EVALUATION_SPLIT')
    signals=classify(panel,policy,macro_report); scenarios=[]; comparisons=[]
    for cost in costs:
        daily=simulate(panel,signals,evaluation_start,cost,policy)
        periods={}
        for name,rows in (('development',[r for r in daily if r['end_ms']<=holdout_start]),
                          ('evaluation',[r for r in daily if r['decision_ms']>=holdout_start])):
            comp=block_comparison(rows,samples=bootstrap_samples); comparisons.append(comp)
            periods[name]=dict(start_ms=rows[0]['decision_ms'] if rows else None,
                end_ms=rows[-1]['end_ms'] if rows else None,
                portfolios={s:metrics(rows,s) for s in ('adaptive','regime_equal','fixed_50','buy_hold_100','cash')},
                regimes={g:dict(days=sum(r['regime']==g for r in rows),
                    mean_risk_cap=float(np.mean([r['risk_cap'] for r in rows if r['regime']==g]))
                        if any(r['regime']==g for r in rows) else None) for g in REGIMES},
                adaptive_vs_fixed_50=comp)
        scenarios.append(dict(one_way_cost_bps=cost,periods=periods,daily=daily))
    valid=sorted([c for c in comparisons if 'one_sided_p' in c],key=lambda c:c['one_sided_p'])
    adjusted=0.
    for i,c in enumerate(valid):
        adjusted=max(adjusted,min(1.,c['one_sided_p']*(len(valid)-i)))
        c['holm_adjusted_p']=adjusted
    latest=signals[-1]
    return dict(schema='four-regime-research-v1',mode='RESEARCH_ONLY',live_enabled=False,
        status='EXPLORATORY_HISTORICAL_RESULTS',price_quality='HISTORICAL_SNAPSHOT',asof_ms=asof,
        direction_basis='MIXED_7D_FORECAST' if macro_report is not None else 'PRICE_ONLY_BASELINE',
        data_start_ms=int(times[0]),data_end_ms=int(times[-1]),
        current_status='STALE_HISTORY' if asof-int(times[-1])>3*DAY else 'UNVALIDATED_SNAPSHOT',
        historical_last_regime=latest,policy=asdict(policy),risk_caps=CAPS,
        scenarios=scenarios,promotion='BLOCKED_SNAPSHOT_DATA_AND_FORWARD_VALIDATION',
        limitations=['Reference prices are not exchange fills; no intraday liquidity model',
            'Two-day signal lag is an assumption, not archived availability proof',
            'No macro benefit is claimed by a price-only run',
            'Cash yield, tax and FX omitted; two fixed USD assets, no shorting or leverage',
            'VPD, FAST and Q strategies require their own aligned net-return histories'])
