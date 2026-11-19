"""通用工具：会计期间（YYYY-MM）运算与数值格式化。"""
from __future__ import annotations

from datetime import datetime


def period_add(period: str, months: int) -> str:
    """期间加减月数，例如 period_add('2024-01', 1) == '2024-02'。"""
    year, month = (int(x) for x in period.split("-"))
    idx = (year * 12 + (month - 1)) + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def period_range(start: str, end_inclusive: str) -> list[str]:
    """返回闭区间内的全部月度期间。"""
    out: list[str] = []
    cur = start
    while cur <= end_inclusive:
        out.append(cur)
        cur = period_add(cur, 1)
    return out


def compare_period(a: str, b: str) -> int:
    """期间比较：a<b 返回 -1，相等 0，a>b 1。"""
    ya, ma = (int(x) for x in a.split("-"))
    yb, mb = (int(x) for x in b.split("-"))
    return (ya * 12 + ma) - (yb * 12 + mb)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def fmt_int(n: float | int | None) -> str:
    if n is None:
        return "—"
    return f"{round(n):,}"


def fmt_delta(n: float | int | None) -> str:
    if n is None:
        return "—"
    sign = "+" if n >= 0 else "−"
    return f"{sign}{round(abs(n)):,}"


def pct(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "+" if n >= 0 else "−"
    return f"{sign}{abs(n) * 100:.1f}%"
