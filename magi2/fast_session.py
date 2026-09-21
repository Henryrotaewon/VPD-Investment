"""FAST reporting/trading day: 07:30 Asia/Seoul, without resetting capital."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
OFFSET = timedelta(hours=7, minutes=30)


def day(ts):
    return (datetime.fromtimestamp(ts / 1000, KST) - OFFSET).date().isoformat()


def bounds(date):
    start = datetime.fromisoformat(date).replace(tzinfo=KST) + OFFSET
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)
