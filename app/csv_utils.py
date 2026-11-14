"""CSV 底表解析与校验。

接受的 CSV 格式（首行表头，列名大小写/空格不敏感）::

    group,count
    A组,2
    B组,10

要求：
* 2~16 个唯一组名（非空、去空格后不重复）；
* 人数为 0~1000 的整数；
* 总人数由各组人数求和得到，视为公开信息。
"""

from __future__ import annotations

import csv
import io

MIN_GROUPS = 2
MAX_GROUPS = 16
MIN_COUNT = 0
MAX_COUNT = 1000


class CSVValidationError(ValueError):
    """CSV 内容不符合底表要求。"""


def parse_base_table(raw: bytes | str) -> tuple[list[tuple[str, int]], int]:
    """解析并校验 CSV，返回 ``([(组名, 人数), ...], 总人数)``。"""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CSVValidationError("CSV 必须为 UTF-8 编码") from exc
    else:
        text = raw

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise CSVValidationError("CSV 至少需要表头行和一行数据")

    header = [cell.strip().lower() for cell in rows[0]]
    if len(header) < 2:
        raise CSVValidationError("表头需包含两列：组名列与人数列")

    def _find_col(*candidates: str) -> int:
        for cand in candidates:
            if cand in header:
                return header.index(cand)
        raise CSVValidationError(
            f"表头缺少列 {candidates}，实际表头：{rows[0]!r}"
        )

    name_col = _find_col("group", "group_name", "name", "组名", "名称")
    count_col = _find_col("count", "size", "cases", "人数", "病例数")

    groups: list[tuple[str, int]] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) <= max(name_col, count_col):
            raise CSVValidationError(f"第 {line_no} 行列数不足：{row!r}")
        name = row[name_col].strip()
        count_raw = row[count_col].strip()
        if not name:
            raise CSVValidationError(f"第 {line_no} 行组名为空")
        if name in seen:
            raise CSVValidationError(f"组名重复：{name}")
        try:
            count = int(count_raw)
        except ValueError as exc:
            raise CSVValidationError(
                f"第 {line_no} 行人数不是整数：{count_raw!r}"
            ) from exc
        if not (MIN_COUNT <= count <= MAX_COUNT):
            raise CSVValidationError(
                f"第 {line_no} 行人数 {count} 超出允许范围 "
                f"[{MIN_COUNT}, {MAX_COUNT}]：组 {name}"
            )
        seen.add(name)
        groups.append((name, count))

    if not (MIN_GROUPS <= len(groups) <= MAX_GROUPS):
        raise CSVValidationError(
            f"唯一组名数量需在 {MIN_GROUPS}~{MAX_GROUPS} 之间，当前为 {len(groups)}"
        )

    total = sum(size for _, size in groups)
    return groups, total
