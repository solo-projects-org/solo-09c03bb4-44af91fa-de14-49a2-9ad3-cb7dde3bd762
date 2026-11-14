"""组合泄露核查（基于 SciPy HiGHS 混合整数线性规划）。

建模
----
把每个组的人数视为未知的非负整数 x_0, ..., x_{n-1}。
攻击者已知的全部信息仅来自：

1. 总人数（公开）：``sum(x) == total``；
2. 历次已公布合计：对每条发布记录，选中组的 ``sum(x) == published_total``；
3. 本次申请的合计：同样是一个等式约束。

对每个保密组（真实人数 1~4 的组）分别最小化、最大化其变量。
若在全部可行整数解中取值唯一（最小值 == 最大值），则该组人数已被确定，
本次发布会造成泄露，必须拒绝。

求解器状态不是“已找到最优解”时视为无定论（inconclusive），调用方应拒绝发布。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

# 与整数解比较时的数值容差。HiGHS 对整数变量返回的最优值即整数，留少量余量。
_INT_TOL = 1e-6
# 单次 milp 调用的时间上限（秒），超时按无定论处理。
_TIME_LIMIT = 30.0


class InconclusiveError(RuntimeError):
    """求解器未能给出确定结论，按“不发布”处理。"""


@dataclass
class CheckResult:
    """核查结果。

    ``leaked_groups`` 非空即应拒绝；``inconclusive`` 为真时同样不得发布。
    """

    feasible: bool
    leaked_groups: list[str] = field(default_factory=list)
    inconclusive: bool = False
    reason: str | None = None


def _solve_extreme(
    n: int,
    objective_index: int,
    direction: int,
    constraints: LinearConstraint,
    integrality: np.ndarray,
    bounds: Bounds,
) -> float:
    """求第 ``objective_index`` 个变量在可行域上的最小（direction=1）或最大（direction=-1）值。"""
    c = np.zeros(n)
    c[objective_index] = float(direction)

    result = milp(
        c=c,
        constraints=constraints,
        integrality=integrality,
        bounds=bounds,
        options={"time_limit": _TIME_LIMIT, "disp": False},
    )

    # 0 = Optimal。其余状态（不可行、超时、数值问题等）都不能给出确定结论。
    if result.status != 0 or result.x is None or result.fun is None:
        raise InconclusiveError(
            f"MILP 求解无定论 (status={result.status}, message={result.message!r})"
        )
    return float(result.x[objective_index])


def check_publication(
    group_names: list[str],
    secret_flags: list[bool],
    total: int,
    history_selections: list[list[str]],
    history_totals: list[int],
    current_selection: list[str],
    current_total: int,
) -> CheckResult:
    """核查一次发布申请是否会泄露任何保密组的人数。

    参数
    ----
    group_names:
        底表中的全部组名，顺序即变量索引顺序。
    secret_flags:
        与 ``group_names`` 等长，True 表示该组真实人数在 1~4 之间，需要保密。
    total:
        公开的总人数。
    history_selections / history_totals:
        历次已发布记录：每次选中的组名列表与当时公布的合计，一一对应。
    current_selection / current_total:
        本次申请选中的组与申请公布的合计。

    返回 :class:`CheckResult`。任何求解异常都收敛为 inconclusive，不向调用方抛出。
    """
    n = len(group_names)
    if n == 0:
        return CheckResult(feasible=False, reason="底表为空")

    index = {name: i for i, name in enumerate(group_names)}
    try:
        current_idx = [index[name] for name in current_selection]
        history_idx = [[index[name] for name in sel] for sel in history_selections]
    except KeyError as exc:
        return CheckResult(feasible=False, reason=f"未知组名: {exc.args[0]}")

    # 约束矩阵 A x = b：一行总人数 + 每行一次发布（历史 + 本次）。
    rows: list[np.ndarray] = []
    rhs: list[float] = []

    total_row = np.ones(n)
    rows.append(total_row)
    rhs.append(float(total))

    for idx, value in zip(history_idx, history_totals):
        row = np.zeros(n)
        row[idx] = 1.0
        rows.append(row)
        rhs.append(float(value))

    cur_row = np.zeros(n)
    cur_row[current_idx] = 1.0
    rows.append(cur_row)
    rhs.append(float(current_total))

    A = np.vstack(rows)
    b = np.array(rhs)
    # 全部为等式约束：lb == ub == b。
    constraints = LinearConstraint(A, b, b)
    # 所有变量取非负整数。上界不额外收紧——非负性 + 总和等式已隐含 x_i <= total，
    # 不能把“真实人数 0~1000”这类底表私有信息加进攻击者模型。
    integrality = np.ones(n)
    bounds = Bounds(lb=np.zeros(n), ub=np.full(n, np.inf))

    try:
        # 先对任一变量求一次解，确认约束系统可行（正常情况下必然可行，
        # 因为真实底表本身就是一组解）。
        first_secret = next((i for i, flag in enumerate(secret_flags) if flag), None)
        probe = (
            first_secret if first_secret is not None else 0
        )
        _solve_extreme(n, probe, 1, constraints, integrality, bounds)

        leaked: list[str] = []
        for i, is_secret in enumerate(secret_flags):
            if not is_secret:
                continue
            lo = _solve_extreme(n, i, 1, constraints, integrality, bounds)
            hi = _solve_extreme(n, i, -1, constraints, integrality, bounds)
            if abs(hi - lo) <= _INT_TOL:
                leaked.append(group_names[i])
    except InconclusiveError as exc:
        return CheckResult(
            feasible=False,
            inconclusive=True,
            reason=str(exc),
        )

    return CheckResult(feasible=True, leaked_groups=leaked)
