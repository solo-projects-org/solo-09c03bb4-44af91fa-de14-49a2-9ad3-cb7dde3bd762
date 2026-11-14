"""checker 的单元测试：组合泄露、补集泄露、安全发布与无泄露判定。"""

import pytest

from app.checker import check_publication

NAMES = ["A", "B", "C", "D"]
# A=2 为保密组（1~4），其余非保密；总人数 100 公开。
SIZES = [2, 10, 20, 68]
SECRET = [True, False, False, False]
TOTAL = 100


def test_single_publication_is_safe():
    # 只发布 B+C=30：A 可在 0..70 间取值，不唯一。
    result = check_publication(NAMES, SECRET, TOTAL, [], [], ["B", "C"], 30)
    assert result.feasible and not result.inconclusive
    assert result.leaked_groups == []


def test_complement_leak_rejected():
    # 只发布 A：约束 x_A = 2，保密组 A 被直接确定。
    result = check_publication(NAMES, SECRET, TOTAL, [], [], ["A"], 2)
    assert result.leaked_groups == ["A"]


def test_combination_leak_across_history():
    """三步两两合计差分攻击：前两次单独看都安全，第三次联立后确定 A。

    发布 1: B+C = 30
    发布 2: A+C = 22   （A 仍可在 0..22 内变化）
    发布 3: A+B = 12   （三式联立：A = (22+12-30)/2 = 2，唯一确定）
    """
    r1 = check_publication(NAMES, SECRET, TOTAL, [], [], ["B", "C"], 30)
    assert r1.leaked_groups == []

    r2 = check_publication(
        NAMES, SECRET, TOTAL,
        [["B", "C"]], [30],
        ["A", "C"], 22,
    )
    assert r2.leaked_groups == []

    r3 = check_publication(
        NAMES, SECRET, TOTAL,
        [["B", "C"], ["A", "C"]], [30, 22],
        ["A", "B"], 12,
    )
    assert r3.leaked_groups == ["A"]


def test_total_plus_one_publish_can_leak_secret():
    """总人数本身也是约束：发布除 A 外所有组的合计即补集，可反推 A。"""
    result = check_publication(
        NAMES, SECRET, TOTAL, [], [], ["B", "C", "D"], 98
    )
    assert result.leaked_groups == ["A"]


def test_multiple_secret_groups_all_flagged():
    names = ["S1", "S2", "X"]
    sizes = [3, 4, 93]
    secret = [True, True, False]
    # 发布 S1+S2=7 且发布 S1=3 -> 两个保密组都被确定。
    r = check_publication(
        names, secret, 100, [["S1"]], [3], ["S1", "S2"], 7
    )
    assert set(r.leaked_groups) == {"S1", "S2"}


def test_zero_size_group_is_not_secret():
    names = ["Z", "X", "Y"]
    sizes = [0, 40, 60]
    secret = [False, False, False]
    r = check_publication(names, secret, 100, [], [], ["Z"], 0)
    assert r.leaked_groups == []


def test_unknown_group_returns_infeasible():
    r = check_publication(NAMES, SECRET, TOTAL, [], [], ["NOPE"], 1)
    assert not r.feasible and r.reason
