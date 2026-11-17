"""统一的业务时点：带时区的本地时间字符串，审计记录可排序、可回查。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")
