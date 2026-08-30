"""Measure VWPI5 incremental effect on the frozen 200-market replay dataset.
Control = VPDReplay>=65 & MaxPriceReturn<=10.
Tests add VWPI thresholds. No VPD production scoring changes.
"""
from pathlib import Path
import json
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'data'/'vwpi5_validation_200.csv'
OUT=ROOT/'data'/'vwpi5_incremental_effect_latest.json'

def stats(g):
    o={'N':int(len(g))}
    for c in ['D1%','D3%','D5%','MFE5%','MAE5%']:
        z=pd.to_numeric(g[c],errors='coerce').dropna()
        o['Avg'+c]=round(float(z.mean()),2) if len(z) else None
        o['Median'+c]=round(float(z.median()),2) if len(z) else None
    for n in [1,3,5]:
        z=pd.to_numeric(g[f'D{n}%'],errors='coerce').dropna()
        o[f'D{n}WinRate%']=round(float((z>0).mean()*100),1) if len(z) else None
        o[f'D{n}NonNegative%']=round(float((z>=0).mean()*100),1) if len(z) else None
    z=pd.to_numeric(g['MFE5%'],errors='coerce').dropna()
    for t in [3,5,10,15,20]: o[f'MFE5_ge_{t}%']=round(float((z>=t).mean()*100),1) if len(z) else None
    return o

def delta(a,b):
    keys=['AvgD1%','AvgD3%','AvgD5%','MedianD3%','MedianD5%','D3WinRate%','D5WinRate%','AvgMFE5%','MedianMFE5%','AvgMAE5%','MedianMAE5%','MFE5_ge_10%','MFE5_ge_15%','MFE5_ge_20%']
    return {k:round(b[k]-a[k],2) if a.get(k) is not None and b.get(k) is not None else None for k in keys}

def main():
    df=pd.read_csv(SRC)
    control=df[(df.VPDReplay>=65)&(df['MaxPriceReturn%']<=10)].copy()
    cs=stats(control)
    tests={}
    for t in [0,10,20,30,40]:
        g=control[control.VWPI5>=t]
        s=stats(g); tests[f'VWPI{t}+']={'stats':s,'delta_vs_control':delta(cs,s)}
    # Exclusive VWPI bands test whether outcome improves progressively rather than merely by shrinking N.
    bands=[]
    cuts=[(-999,0,'VWPI<0'),(0,10,'0-10'),(10,20,'10-20'),(20,30,'20-30'),(30,40,'30-40'),(40,999,'40+')]
    for lo,hi,name in cuts:
        g=control[(control.VWPI5>=lo)&(control.VWPI5<hi)]
        bands.append({'band':name,**stats(g)})
    # Complement comparison is especially useful: same VPD/Max universe, VWPI20+ versus VWPI<20.
    pos=control[control.VWPI5>=20]; neg=control[control.VWPI5<20]
    ps,ns=stats(pos),stats(neg)
    doc={'name':'VWPI5 Incremental Effect Test','source_rows':int(len(df)),'control_rule':'VPDReplay>=65 AND MaxPriceReturn<=10','control':cs,'threshold_tests':tests,'exclusive_bands':bands,'direct_comparison':{'VWPI20+':ps,'VWPI<20':ns,'delta_20plus_minus_below20':delta(ns,ps)},'interpretation_rule':['Primary question: does VWPI20+ improve D3/D5 and MFE versus the SAME VPD>=65 & Max<=10 control?','Win rate alone is not sufficient; inspect average + median returns, MFE hit rates and MAE.','A broadly monotonic exclusive-band relationship strengthens evidence of independent VWPI information.']}
    OUT.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(doc,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
