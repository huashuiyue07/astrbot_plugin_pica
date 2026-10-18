"""
cron 表达式匹配（5 字段：分 时 日 月 星期）

为「排行榜定时推送」提供刷新时间配置。只实现常见语法，不引入额外依赖：

===== ================
字段   取值范围
===== ================
分钟   0-59
小时   0-23
日     1-31
月     1-12
星期   0-7（0 与 7 都表示周日）
===== ================

每个字段支持 ``*``（任意）、``a``（定值）、``a-b``（范围）、``a,b,c``（枚举）、
``*/n``、``a-b/n`` 与 ``a/n``（步进）。其中 ``a/n`` 与 Vixie cron 一致：
表示从 ``a`` 开始按步进走到该字段上限（例如分钟字段 ``5/10`` = 5,15,25,…,55）。

注意：日与星期**同时**被限定时按「同时满足」处理（AND），与部分 cron 实现的
「任一满足」（OR）不同；只限制其中一个、另一个写 ``*`` 时两者行为一致。
"""

import re
from datetime import datetime

# 字段名与取值范围（顺序即 分 时 日 月 星期）
_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("分钟", 0, 59),
    ("小时", 0, 23),
    ("日", 1, 31),
    ("月", 1, 12),
    ("星期", 0, 7),
)

_DIGITS = re.compile(r"^\d+$")


class CronError(ValueError):
    """cron 表达式非法"""


def _parse_field(text: str, name: str, lo: int, hi: int) -> set[int]:
    """解析单个字段为允许值集合"""
    if not text:
        raise CronError(f"{name}字段为空")
    values: set[int] = set()
    for raw in text.split(","):
        part = raw.strip()
        if not part:
            raise CronError(f"{name}字段含空项: {text}")

        step = 1
        has_step = "/" in part
        if has_step:
            part, _, step_text = part.partition("/")
            if not _DIGITS.match(step_text) or int(step_text) <= 0:
                raise CronError(f"{name}步进非法: {raw}")
            step = int(step_text)

        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            if not _DIGITS.match(start_text) or not _DIGITS.match(end_text):
                raise CronError(f"{name}范围非法: {raw}")
            start, end = int(start_text), int(end_text)
        elif _DIGITS.match(part):
            start = int(part)
            # 与 Vixie cron 一致：带步进的 "a/n" 表示从 a 开始按步进走到字段上限
            # （分钟字段 "5/10" = 5,15,25,35,45,55；"5/1" = 5..59），
            # 不带步进时仍是单值 {a}
            end = hi if has_step else start
        else:
            raise CronError(f"{name}取值非法: {raw}")

        if start > end:
            raise CronError(f"{name}范围起点大于终点: {raw}")
        if start < lo or end > hi:
            raise CronError(f"{name}超出范围 {lo}-{hi}: {raw}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> tuple[frozenset[int], ...]:
    """解析 5 字段 cron 表达式，返回各字段的允许值集合；非法时抛 CronError"""
    fields = str(expr or "").split()
    if len(fields) != 5:
        raise CronError(f"需要 5 个字段（分 时 日 月 星期），实际 {len(fields)} 个: {expr!r}")
    parsed: list[frozenset[int]] = []
    for text, (name, lo, hi) in zip(fields, _FIELDS):
        values = _parse_field(text, name, lo, hi)
        if name == "星期":
            values = {v % 7 for v in values}  # 7 与 0 都表示周日
        parsed.append(frozenset(values))
    return tuple(parsed)


def validate_cron(expr: str) -> str | None:
    """校验表达式：合法返回 None，非法返回错误说明"""
    try:
        parse_cron(expr)
    except CronError as e:
        return str(e)
    return None


def cron_matches(expr: str, when: datetime) -> bool:
    """``when`` 是否命中 cron 表达式；表达式非法时抛 CronError"""
    minute, hour, day, month, weekday = parse_cron(expr)
    # Python 的 weekday(): 周一=0 … 周日=6；cron: 周日=0 … 周六=6
    cron_weekday = (when.weekday() + 1) % 7
    return (
        when.minute in minute
        and when.hour in hour
        and when.day in day
        and when.month in month
        and cron_weekday in weekday
    )
