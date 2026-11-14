"""FastAPI 应用：罕见病人群组合计发布的组合泄露核查服务。

并发模型
--------
发布申请在进程级 ``threading.RLock`` 内串行处理：每个请求都基于
“截至它实际取得锁时”的累计历史重新核查，因此并发申请按实际发布次序
逐条接受/拒绝，不可能各自通过后共同泄密。锁内同时完成 MILP 核查与
SQLite 写入，拒绝、无定论或保存失败时历史保持不变。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .checker import check_publication
from .csv_utils import CSVValidationError, parse_base_table
from .database import Database

DB_PATH = os.environ.get("APP_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

# 保密阈值：真实人数落在 [1, 4] 的组需要保密（0 人与 ≥5 人组不在保护范围）。
SECRET_MIN = 1
SECRET_MAX = 4

app = FastAPI(title="群组合计发布泄露核查", version="1.0.0")
db = Database(DB_PATH)
# 发布/上传串行化。RLock 便于同一线程内嵌套调用。
_publish_lock = threading.RLock()


class PublishRequest(BaseModel):
    groups: list[str] = Field(..., min_length=1, description="本次申请公布合计所勾选的组名")


def _state_payload() -> dict:
    base = db.get_base_table()
    if base is None:
        return {"initialized": False, "groups": [], "total": None, "history": []}
    names, sizes, total = base
    groups = [
        {
            "name": name,
            "size": size,
            "secret": SECRET_MIN <= size <= SECRET_MAX,
        }
        for name, size in zip(names, sizes)
    ]
    return {
        "initialized": True,
        "groups": groups,
        "total": total,
        "history": db.list_publications(),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def get_state() -> dict:
    return _state_payload()


@app.post("/api/upload")
def upload_csv(file: UploadFile = File(...)) -> dict:
    raw = file.file.read()
    try:
        groups, total = parse_base_table(raw)
    except CSVValidationError as exc:
        raise HTTPException(status_code=400, detail=f"CSV 校验失败：{exc}") from exc

    with _publish_lock:
        if db.has_base_table():
            raise HTTPException(status_code=409, detail="底表已固定，不能重复上传；历史数据沿用")
        try:
            db.insert_base_table(groups, total)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _state_payload()


@app.post("/api/publish")
def publish(req: PublishRequest) -> dict:
    with _publish_lock:
        base = db.get_base_table()
        if base is None:
            raise HTTPException(status_code=409, detail="底表尚未上传，无法受理发布申请")
        names, sizes, total = base

        selection = req.groups
        if len(selection) != len(set(selection)):
            raise HTTPException(status_code=400, detail="勾选的组名存在重复")
        unknown = [g for g in selection if g not in set(names)]
        if unknown:
            raise HTTPException(status_code=400, detail=f"存在未知组名：{unknown}")

        # 本次合计直接由底表计算（合计本身就是申请公布的数字，
        # 核查关注的是它与历次合计联立后能否反推出保密组人数）。
        size_of = dict(zip(names, sizes))
        current_total = sum(size_of[name] for name in selection)

        history = db.list_publications()
        result = check_publication(
            group_names=names,
            secret_flags=[SECRET_MIN <= s <= SECRET_MAX for s in sizes],
            total=total,
            history_selections=[pub["selected_groups"] for pub in history],
            history_totals=[pub["published_total"] for pub in history],
            current_selection=selection,
            current_total=current_total,
        )

        if result.inconclusive:
            # 求解无定论：不发布，历史不变。
            raise HTTPException(
                status_code=503,
                detail=f"整数解核查无定论，出于保密不予发布：{result.reason}",
            )
        if result.leaked_groups:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "发布被拒绝：联立全部约束后，以下保密组的人数在所有可行整数底表中取值唯一，会泄露小组人数",
                    "leaked_groups": result.leaked_groups,
                    "history_unchanged": True,
                },
            )

        try:
            record = db.insert_publication(selection, current_total)
        except Exception as exc:  # 保存失败：不发布，历史不变
            raise HTTPException(
                status_code=500, detail=f"保存发布记录失败，本次合计未发布：{exc}"
            ) from exc

        return {
            "accepted": True,
            "publication": record,
            "state": _state_payload(),
        }
