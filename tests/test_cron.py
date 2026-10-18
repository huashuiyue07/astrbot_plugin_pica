"""cron 表达式匹配测试（core/cron.py）。

日期基准（用于固定断言）：
- 2026-08-03 周一
- 2026-08-09 周日
- 2026-08-01 周六
"""

from datetime import datetime

import pytest

from core.cron import CronError, cron_matches, parse_cron, validate_cron


def _dt(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute)


def test_date_fixtures_are_stable():
    assert _dt(2026, 8, 3).weekday() == 0  # 周一
    assert _dt(2026, 8, 9).weekday() == 6  # 周日


# ---------------------------------------------------------------- 正常表达式


def test_every_minute_matches():
    assert cron_matches("* * * * *", _dt(2026, 8, 3, 12, 34))


def test_fixed_time_matches_only_that_minute():
    expr = "0 9 * * *"
    assert cron_matches(expr, _dt(2026, 8, 3, 9, 0))
    assert not cron_matches(expr, _dt(2026, 8, 3, 9, 1))
    assert not cron_matches(expr, _dt(2026, 8, 3, 10, 0))


def test_minute_step():
    expr = "*/15 * * * *"
    for minute in (0, 15, 30, 45):
        assert cron_matches(expr, _dt(2026, 8, 3, 0, minute))
    assert not cron_matches(expr, _dt(2026, 8, 3, 0, 7))


def test_bare_value_with_step_follows_vixie_semantics():
    """`a/n` = 从 a 开始按步进走到字段上限（Vixie cron 语义）"""
    assert parse_cron("5/10 * * * *")[0] == frozenset({5, 15, 25, 35, 45, 55})
    assert parse_cron("0 2/6 * * *")[1] == frozenset({2, 8, 14, 20})

    assert cron_matches("5/10 * * * *", _dt(2026, 8, 3, 0, 25))
    assert not cron_matches("5/10 * * * *", _dt(2026, 8, 3, 0, 7))


def test_bare_value_with_step_at_field_upper_bound():
    assert parse_cron("59/10 * * * *")[0] == frozenset({59})


def test_bare_value_without_step_is_single():
    assert parse_cron("5 * * * *")[0] == frozenset({5})
    # a/1 等价于 a..上限（与 Vixie 一致）
    assert parse_cron("5/1 * * * *")[0] == frozenset(range(5, 60))


def test_hour_range_step():
    expr = "0 0-23/6 * * *"
    for hour in (0, 6, 12, 18):
        assert cron_matches(expr, _dt(2026, 8, 3, hour, 0))
    assert not cron_matches(expr, _dt(2026, 8, 3, 5, 0))


def test_hour_range_and_list():
    assert cron_matches("0 9-17 * * *", _dt(2026, 8, 3, 17, 0))
    assert not cron_matches("0 9-17 * * *", _dt(2026, 8, 3, 18, 0))
    assert cron_matches("0 9,18 * * *", _dt(2026, 8, 3, 18, 0))
    assert not cron_matches("0 9,18 * * *", _dt(2026, 8, 3, 12, 0))


def test_weekday_numbers():
    monday = _dt(2026, 8, 3, 9, 0)
    sunday = _dt(2026, 8, 9, 9, 0)
    assert cron_matches("0 9 * * 1", monday)  # 1 = 周一
    assert not cron_matches("0 9 * * 1", sunday)
    assert cron_matches("0 9 * * 1-5", monday)


def test_sunday_accepts_both_0_and_7():
    sunday = _dt(2026, 8, 9, 9, 0)
    assert cron_matches("0 9 * * 0", sunday)
    assert cron_matches("0 9 * * 7", sunday)
    assert not cron_matches("0 9 * * 6", sunday)  # 6 = 周六


def test_day_of_month():
    assert cron_matches("0 9 1 * *", _dt(2026, 8, 1, 9, 0))
    assert not cron_matches("0 9 1 * *", _dt(2026, 8, 2, 9, 0))


def test_month():
    assert cron_matches("0 0 1 1 *", _dt(2026, 1, 1, 0, 0))
    assert not cron_matches("0 0 1 1 *", _dt(2026, 2, 1, 0, 0))


def test_parse_cron_returns_allowed_values():
    minute, hour, day, month, weekday = parse_cron("*/15 9 * * 1")
    assert minute == frozenset({0, 15, 30, 45})
    assert hour == frozenset({9})
    assert day == frozenset(range(1, 32))
    assert month == frozenset(range(1, 13))
    assert weekday == frozenset({1})


def test_whitespace_is_tolerated():
    assert cron_matches("  0   9  *  *  * ", _dt(2026, 8, 3, 9, 0))


# ---------------------------------------------------------------- 非法表达式


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "0 9 * *",  # 字段不足
        "0 9 * * * *",  # 字段过多
        "60 * * * *",  # 分钟越界
        "0 24 * * *",  # 小时越界
        "0 9 0 * *",  # 日越界
        "0 9 * 13 *",  # 月越界
        "0 9 * * 8",  # 星期越界
        "*/0 * * * *",  # 步进为 0
        "a * * * *",  # 非数字
        "0 9-1 * * *",  # 范围起点大于终点
        "0 9, * * *",  # 空枚举项
    ],
)
def test_invalid_expressions_are_reported(expr):
    assert validate_cron(expr) is not None


def test_valid_expression_reports_nothing():
    assert validate_cron("0 9 * * 1") is None


def test_cron_matches_raises_on_invalid_expression():
    with pytest.raises(CronError):
        cron_matches("bad expr", _dt(2026, 8, 3, 9, 0))
