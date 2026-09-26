"""Export actual out-of-sample price/return comparisons; never generate dummy data."""
from datetime import datetime, timezone
from pathlib import Path
import numpy as np


def render(report, destination):
    rows = [r for r in report['forecasts'] if r['horizon_days']==1 and r['actual_log_return'] is not None]
    if not rows:
        raise ValueError('NO_OBSERVED_OUT_OF_SAMPLE_RESULTS')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2,2,figsize=(13,8),layout='constrained')
    for col, asset in enumerate(('BTC','ETH')):
        sample = sorted((r for r in rows if r['asset']==asset),key=lambda r:r['end_ms'])
        if not sample:
            for ax in axes[:,col]: ax.text(.5,.5,'No observed results',ha='center',transform=ax.transAxes)
            continue
        dates = [datetime.fromtimestamp(r['end_ms']/1000,timezone.utc) for r in sample]
        start = np.array([r['start_price'] for r in sample]); end = np.array([r['end_price'] for r in sample])
        forecast = np.exp([r['predictions']['mixed'] for r in sample])*start
        scale = 100/start[0]
        axes[0,col].plot(dates,end*scale,label='Observed price')
        axes[0,col].plot(dates,forecast*scale,label='Forecast made 1 day earlier',alpha=.75)
        axes[0,col].plot(dates,start*scale,label='No-change baseline',ls=':',alpha=.7)
        axes[0,col].set_title(asset+' · price (first reference = 100)')
        actual = np.expm1([r['actual_log_return'] for r in sample])*100
        pred = np.expm1([r['predictions']['mixed'] for r in sample])*100
        axes[1,col].plot(dates,actual,label='Observed next-day return')
        axes[1,col].plot(dates,pred,label='Predicted next-day return',alpha=.75)
        axes[1,col].axhline(0,color='grey',lw=.5)
        axes[1,col].set_title(asset+' · return (%)')
        for ax in axes[:,col]:
            ax.grid(alpha=.2); ax.legend(fontsize=8); ax.tick_params(axis='x',rotation=25)
    fig.suptitle('Research only · chronological out-of-sample observations · UTC outcome dates')
    fig.supxlabel('Price curves share a known starting price; visual similarity is not evidence of predictive skill.\nNo trading P&L. Seven-day overlapping forecasts are evaluated separately.')
    Path(destination).parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(destination,dpi=160); plt.close(fig)
