# ============================================================
# VPD Scanner Integrated v1.6
# SOURCE OF TRUTH: Upbit KRW Spot OHLCV ONLY
# VPD SCORE: unchanged from v1.5
# v1.6 adds VWPI5 Confidence as a SEPARATE evidence layer.
# ============================================================
from pathlib import Path

BASE = Path(__file__).with_name("vpd_scanner_v1_5.py")
source = BASE.read_text(encoding="utf-8")
source = source.replace("VPD Scanner Integrated v1.5", "VPD Scanner Integrated v1.6")
source = source.replace('User-Agent":"VPD-Scanner/1.5"', 'User-Agent":"VPD-Scanner/1.6"')
source = source.replace('VPD Scanner v1.5 시작', 'VPD Scanner v1.6 시작')
# No score/trigger patch here: v1.6 intentionally inherits v1.5 scoring exactly.
exec(compile(source, str(BASE), "exec"), globals())
