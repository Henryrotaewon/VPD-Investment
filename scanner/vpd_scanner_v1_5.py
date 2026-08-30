# ============================================================
# VPD Scanner Integrated v1.5
# SOURCE OF TRUTH: Upbit KRW Spot OHLCV ONLY
#
# v1.5 change from v1.4:
# - Price non-overheating is evaluated with the day's maximum price reaction
#   versus previous close, not current return alone.
# - MaxPriceReturn% = max(today high, current) / previous close - 1
# - Giveback%p = MaxPriceReturn% - current 1D%
# - Trigger B uses MaxPriceReturn% <= 6% for the price-muted condition.
#
# The v1.4 source remains preserved as the historical baseline.
# This v1.5 entrypoint loads that baseline and promotes the approved change
# into the production scanner version.
# ============================================================

from pathlib import Path

BASE = Path(__file__).with_name("vpd_scanner_v1_4.py")
source = BASE.read_text(encoding="utf-8")

source = source.replace("VPD Scanner Integrated v1.4", "VPD Scanner Integrated v1.5")
source = source.replace('User-Agent":"VPD-Scanner/1.4"', 'User-Agent":"VPD-Scanner/1.5"')
source = source.replace('VPD Scanner v1.4 시작', 'VPD Scanner v1.5 시작')

old_line = 'latest=df.iloc[-1]; prev=df.iloc[-2]; price=float(latest["trade_price"]); prev_close=float(prev["trade_price"]); day_return=(price/prev_close-1)*100'
new_line = 'latest=df.iloc[-1]; prev=df.iloc[-2]; price=float(latest["trade_price"]); prev_close=float(prev["trade_price"]); day_return=(price/prev_close-1)*100; day_high=float(latest["high_price"]); max_price=max(price,day_high); max_price_return=(max_price/prev_close-1)*100; giveback_pct_point=max_price_return-day_return'
if old_line not in source:
    raise RuntimeError("v1.5 patch failed: price calculation line not found")
source = source.replace(old_line, new_line, 1)

old_score = 'vpd=score_vpd(vol3_10,today_value_10,intra_accel,rsi_now,william_now,day_return,spike_collapse)'
new_score = 'vpd=score_vpd(vol3_10,today_value_10,intra_accel,rsi_now,william_now,max_price_return,spike_collapse)'
if old_score not in source:
    raise RuntimeError("v1.5 patch failed: score call not found")
source = source.replace(old_score, new_score, 1)

old_trigger = 'elif 75<=vpd<85 and momentum=="↑↑" and abs(day_return)<=6: trigger="B"'
new_trigger = 'elif 75<=vpd<85 and momentum=="↑↑" and abs(max_price_return)<=6: trigger="B"'
if old_trigger not in source:
    raise RuntimeError("v1.5 patch failed: Trigger B line not found")
source = source.replace(old_trigger, new_trigger, 1)

old_row = '"1D%":round(day_return,2),"1H%"'
new_row = '"1D%":round(day_return,2),"MaxPriceReturn%":round(max_price_return,2),"Giveback%p":round(giveback_pct_point,2),"DayHigh":round(day_high,8),"1H%"'
if old_row not in source:
    raise RuntimeError("v1.5 patch failed: output row insertion point not found")
source = source.replace(old_row, new_row, 1)

exec(compile(source, str(BASE), "exec"), globals())
