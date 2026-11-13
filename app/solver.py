"""整数解核查（基于 SciPy 的 HiGHS 混合整数线性规划）。

把各组人数当作未知的非负整数 x_i，仅使用以下三类约束重建可行底表空间：

1. 总人数约束：sum(x) == T（总人数公开）；
2. 历次已公布合计：对每次发布的组集合 S，sum_{i in S} x_i == 该合计；
3. 本次申请合计：同上，把本次申请也临时加入约束。

对每个保密组（真实人数 1~4），尝试寻找与其真实人数不同的可行解。
两个方向都找不到时，说明该组人数在 *全部可行底表* 中被唯一确定，
即本次发布会泄露该组人数。
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

# MILP 结果状态码（scipy.optimize.milp）
_STATUS_OPTIMAL = 0
_STATUS_LIMIT = 1
_STATUS_INFEASIBLE = 2

# 整数判定容差
_INT_TOL = 1e-6


class InconclusiveError(RuntimeError):
    """求解器无法给出确定结论（超时、数值问题等），调用方必须拒绝发布。"""


def _build_constraints(
    group_index: dict[str, int],
    total: int,
    publications: list[dict],
    proposed: tuple[list[str], int] | None,
) -> LinearConstraint:
    """构造总人数 + 历次合计 + 本次合计的等式约束矩阵。"""
    n = len(group_index)
    rows: list[np.ndarray] = []
    values: list[int] = []

    # 1. 总人数约束（公开信息）
    rows.append(np.ones(n, dtype=float))
    values.append(total)

    # 2. 历次已公布合计
    for pub in publications:
        row = np.zeros(n, dtype=float)
        for name in pub["groups"]:
            row[group_index[name]] = 1.0
        rows.append(row)
        values.append(int(pub["aggregate"]))

    # 3. 本次申请合计（临时纳入，不入库）
    if proposed is not None:
        selected, aggregate = proposed
        row = np.zeros(n, dtype=float)
        for name in selected:
            row[group_index[name]] = 1.0
        rows.append(row)
        values.append(int(aggregate))

    a = np.vstack(rows)
    b = np.asarray(values, dtype=float)
    return LinearConstraint(a, b, b)


def _solve(bounds: Bounds, constraints: LinearConstraint) -> object:
    """运行一次 MILP；任何非最优/非不可行的状态都视为无定论。

    可行性问题：目标函数取零向量，只关心是否存在可行整数解。
    """
    n = bounds.lb.shape[0]
    result = milp(
        c=np.zeros(n),
        integrality=np.ones(n),
        bounds=bounds,
        constraints=constraints,
        options={"time_limit": 30.0, "disp": False},
    )
    if result.status not in (_STATUS_OPTIMAL, _STATUS_INFEASIBLE):
        raise InconclusiveError(
            f"整数规划求解无定论（status={result.status}, message={result.message!r}）"
        )
    return result


def check_disclosure(
    group_names: list[str],
    counts: list[int],
    publications: list[dict],
    proposed_groups: list[str],
    proposed_aggregate: int,
) -> list[str]:
    """核查本次申请是否会泄露保密组人数。

    返回被唯一确定（泄露）的保密组组名列表；空列表表示安全，可以发布。
    无法给出确定结论时抛出 InconclusiveError。
    """
    n = len(group_names)
    group_index = {name: i for i, name in enumerate(group_names)}
    total = int(sum(counts))
    constraints = _build_constraints(
        group_index, total, publications, (proposed_groups, proposed_aggregate)
    )

    # 非负整数变量，不额外设上界（0..总人数由总人数约束自然保证）
    base_bounds = Bounds(lb=np.zeros(n), ub=np.full(n, np.inf))

    # 先确认真实底表所在的可行域本身可解（正常情况下恒可行）
    feasibility = _solve(base_bounds, constraints)
    if feasibility.status == _STATUS_INFEASIBLE:  # pragma: no cover - 理论上不会发生
        raise InconclusiveError("约束体系不可行，无法核查")

    exposed: list[str] = []
    sensitive = [
        (i, name)
        for i, name in enumerate(group_names)
        if 1 <= counts[i] <= 4
    ]

    for idx, name in sensitive:
        real_value = counts[idx]
        alternative_found = False

        # 方向一：x_idx >= 真实值 + 1
        lb_up = np.zeros(n)
        lb_up[idx] = real_value + 1
        up_result = _solve(Bounds(lb=lb_up, ub=np.full(n, np.inf)), constraints)
        if up_result.status == _STATUS_OPTIMAL:
            val = int(round(float(up_result.x[idx])))
            if abs(val - real_value) > _INT_TOL:
                alternative_found = True

        # 方向二：x_idx <= 真实值 - 1（真实值为 1 时下界自然为 0）
        if not alternative_found and real_value >= 1:
            ub_down = np.full(n, np.inf)
            ub_down[idx] = max(real_value - 1, 0)
            down_result = _solve(
                Bounds(lb=np.zeros(n), ub=ub_down), constraints
            )
            if down_result.status == _STATUS_OPTIMAL:
                val = int(round(float(down_result.x[idx])))
                if abs(val - real_value) > _INT_TOL:
                    alternative_found = True

        if not alternative_found:
            exposed.append(name)

    return exposed
