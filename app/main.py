"""罕见病人群组合计发布核查 —— FastAPI 入口。

规则摘要：
- 首次上传 CSV 固定底表（2~16 个唯一组名，人数 0~1000 整数）；
- 总人数公开；真实人数 1~4 的组为保密组；
- 发布申请仅以「总人数 + 历次已公布合计 + 本次合计」为约束，
  用 SciPy MILP 枚举可行底表，任何保密组人数被唯一确定即拒绝；
- 核查与落库在同一把进程锁内完成，并发申请按实际发布次序累计；
- 拒绝 / 求解无定论 / 保存失败均不发布，历史不变；
- SQLite 持久化，重启后沿用原底表与历史。
"""

from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .solver import InconclusiveError, check_disclosure


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="罕见病群组合计发布核查", version="1.0.0", lifespan=lifespan)


class PublishRequest(BaseModel):
    groups: list[str] = Field(min_length=1)


def _parse_csv(raw: bytes) -> tuple[list[str], list[int]]:
    """解析并校验底表 CSV，返回 (组名列表, 人数列表)。"""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV 必须为 UTF-8 编码")

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise HTTPException(400, "CSV 内容为空")

    header = [cell.strip() for cell in rows[0]]
    if header[:2] != ["group", "count"]:
        raise HTTPException(
            400, "CSV 首行必须是表头 group,count（可参考样例文件）"
        )

    data_rows = rows[1:]
    if not data_rows:
        raise HTTPException(400, "CSV 缺少数据行")

    names: list[str] = []
    counts: list[int] = []
    seen: set[str] = set()
    for line_no, row in enumerate(data_rows, start=2):
        if len(row) < 2:
            raise HTTPException(400, f"第 {line_no} 行格式错误，应为 group,count")
        name = row[0].strip()
        count_text = row[1].strip()
        if not name:
            raise HTTPException(400, f"第 {line_no} 行组名为空")
        if name in seen:
            raise HTTPException(400, f"组名重复：{name}")
        seen.add(name)
        try:
            count = int(count_text)
        except ValueError:
            raise HTTPException(
                400, f"第 {line_no} 行人数不是整数：{count_text!r}"
            )
        if not 0 <= count <= 1000:
            raise HTTPException(
                400, f"第 {line_no} 行人数越界（允许 0~1000）：{count}"
            )
        names.append(name)
        counts.append(count)

    if not 2 <= len(names) <= 16:
        raise HTTPException(
            400, f"唯一组名数量必须在 2~16 之间，当前为 {len(names)}"
        )
    return names, counts


@app.get("/api/state")
def get_state() -> dict:
    base = db.get_base_table()
    return {
        "initialized": base is not None,
        "base_table": base,
        "publications": db.list_publications(),
    }


@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    names, counts = _parse_csv(raw)

    with db.publish_lock:
        if db.has_base_table():
            raise HTTPException(
                409, "底表已固定，不能再次上传；如需重新演示请先调用重置接口"
            )
        try:
            db.insert_base_table(names, counts)
        except Exception as exc:  # 保存失败：底表保持未初始化状态
            raise HTTPException(500, f"底表保存失败：{exc}")

    return {
        "status": "saved",
        "base_table": db.get_base_table(),
        "publications": db.list_publications(),
    }


@app.post("/api/publish")
def publish(req: PublishRequest) -> dict:
    selected = req.groups

    with db.publish_lock:
        base = db.get_base_table()
        if base is None:
            raise HTTPException(409, "底表尚未上传，无法受理发布申请")

        all_names = [g["name"] for g in base["groups"]]
        name_set = set(all_names)
        if len(selected) != len(set(selected)):
            raise HTTPException(400, "申请中存在重复组名")
        unknown = [name for name in selected if name not in name_set]
        if unknown:
            raise HTTPException(400, f"以下组名不存在于底表：{unknown}")

        count_by_name = {g["name"]: g["count"] for g in base["groups"]}
        aggregate = sum(count_by_name[name] for name in selected)
        publications = db.list_publications()

        try:
            exposed = check_disclosure(
                group_names=all_names,
                counts=[count_by_name[n] for n in all_names],
                publications=publications,
                proposed_groups=selected,
                proposed_aggregate=aggregate,
            )
        except InconclusiveError as exc:
            # 求解无定论：不发布，历史不变
            raise HTTPException(
                409,
                f"求解无定论，本次不予发布，历史保持不变。详情：{exc}",
                headers={"X-Disclosure-Result": "inconclusive"},
            )

        if exposed:
            # 拒绝：不发布，历史不变，并指出组名
            raise HTTPException(
                422,
                "发布被拒绝：在所有可行底表中，以下保密组人数已被唯一确定："
                + "、".join(exposed),
                headers={
                    "X-Disclosure-Result": "rejected",
                    "X-Exposed-Groups": quote(",".join(exposed)),
                },
            )

        try:
            record = db.insert_publication(selected, aggregate)
        except Exception as exc:  # 保存失败：不发布，历史不变
            raise HTTPException(500, f"发布保存失败，本次不予发布：{exc}")

    return {
        "status": "published",
        "publication": record,
        "publications": db.list_publications(),
    }


@app.post("/api/reset")
def reset_demo() -> dict:
    """演示/测试用：清空底表与全部发布历史。"""
    with db.publish_lock:
        db.reset_all()
    return {"status": "reset", "initialized": False, "publications": []}


# ---- 静态网页 ----
_STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


app.mount(
    "/static",
    StaticFiles(directory=str(_STATIC_DIR)),
    name="static",
)
