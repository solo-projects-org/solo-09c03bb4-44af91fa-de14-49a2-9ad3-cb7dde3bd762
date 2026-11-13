"""罕见病研究病例合计发布核查服务。

- 首次上传 CSV 后固定底表（2~16 个唯一组名，人数为 0~1000 的整数）。
- 总人数公开；实际人数为 1~4 的组为保密组。
- 勾选若干组申请公布合计：把各组人数视为未知非负整数，
  仅以总人数、历次已公布合计与本次合计为约束（SciPy MILP 求整数解），
  若任一保密组在全部可行底表中的人数被唯一确定，则拒绝并指出组名。
- 并发申请通过全局锁串行化，按实际发布次序核查累计结果。
- 拒绝 / 求解无定论 / 保存失败时，不发布、历史不变。
- SQLite 持久化，重启后沿用原底表与历史。
"""

import csv
import io
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode

import numpy as np
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from scipy.optimize import Bounds, LinearConstraint, milp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "..", "data", "app.db"))

MIN_GROUPS, MAX_GROUPS = 2, 16
MIN_COUNT, MAX_COUNT = 0, 1000
PROTECTED_LO, PROTECTED_HI = 1, 4  # 实际人数 1~4 的组需要保密

# 发布核查全局锁：并发申请按实际发布次序逐一核查累计结果，
# 防止各自独立通过后共同泄密。
publish_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                name  TEXT PRIMARY KEY,
                count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                groups     TEXT NOT NULL,   -- JSON 数组：本次公布的组名
                total      INTEGER NOT NULL, -- 本次公布的合计
                created_at TEXT NOT NULL
            );
            """
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="罕见病病例合计发布核查", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ---------------------------------------------------------------- 数据访问

def load_base(conn) -> list[tuple[str, int]]:
    rows = conn.execute("SELECT name, count FROM groups ORDER BY rowid").fetchall()
    return [(r["name"], r["count"]) for r in rows]


def load_history(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, groups, total, created_at FROM history ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "groups": json.loads(r["groups"]),
            "total": r["total"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- CSV 解析

def parse_csv(content: bytes) -> list[tuple[str, int]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("文件编码须为 UTF-8")
    rows = [[cell.strip() for cell in r] for r in csv.reader(io.StringIO(text))]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ValueError("CSV 内容为空")

    def looks_like_data(row) -> bool:
        return len(row) >= 2 and row[1].lstrip("+-").isdigit()

    if not looks_like_data(rows[0]):  # 跳过表头（如 group,count）
        rows = rows[1:]
    if not (MIN_GROUPS <= len(rows) <= MAX_GROUPS):
        raise ValueError(
            f"数据行数须为 {MIN_GROUPS}～{MAX_GROUPS} 行，实际 {len(rows)} 行"
        )

    result, seen = [], set()
    for i, row in enumerate(rows, 1):
        if len(row) < 2 or not row[0]:
            raise ValueError(f"第 {i} 行缺少组名")
        name = row[0]
        if name in seen:
            raise ValueError(f"组名重复：{name}")
        seen.add(name)
        try:
            count = int(row[1])
        except ValueError:
            raise ValueError(f"第 {i} 行人数不是整数：{row[1]!r}")
        if not (MIN_COUNT <= count <= MAX_COUNT):
            raise ValueError(f"组「{name}」人数 {count} 超出 {MIN_COUNT}～{MAX_COUNT}")
        result.append((name, count))
    return result


# ---------------------------------------------------------------- 泄露核查

def forced_protected_groups(n, equations, protected_idx):
    """对每个保密组求其在全部可行整数底表中的最小/最大可能人数。

    equations: [([系数...], 右端值)]，等式约束。
    返回 (被唯一确定的保密组下标列表, 是否求解无定论)。
    """
    A = np.array([row for row, _ in equations], dtype=float)
    b = np.array([val for _, val in equations], dtype=float)
    constraints = LinearConstraint(A, b, b)
    bounds = Bounds(np.full(n, float(MIN_COUNT)), np.full(n, float(MAX_COUNT)))
    integrality = np.ones(n)

    forced = []
    for p in protected_idx:
        c = np.zeros(n)
        c[p] = 1.0
        lo_res = milp(c, constraints=constraints, bounds=bounds, integrality=integrality)
        if lo_res.status != 0:
            return [], True
        hi_res = milp(-c, constraints=constraints, bounds=bounds, integrality=integrality)
        if hi_res.status != 0:
            return [], True
        lo, hi = lo_res.fun, -hi_res.fun
        if hi - lo < 0.5:  # 整数解上下界相同 -> 人数被唯一确定
            forced.append(p)
    return forced, False


# ---------------------------------------------------------------- 页面渲染

def render_index(request, base, history, message=None, message_class=""):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "base": base,
            "total": sum(c for _, c in base),
            "history": history,
            "message": message,
            "message_class": message_class,
            "protected_lo": PROTECTED_LO,
            "protected_hi": PROTECTED_HI,
        },
    )


def redirect_with_msg(msg: str, cls: str) -> RedirectResponse:
    return RedirectResponse("/?" + urlencode({"msg": msg, "cls": cls}), status_code=303)


# ---------------------------------------------------------------- 路由

@app.get("/")
def index(request: Request, msg: str = "", cls: str = ""):
    with get_db() as conn:
        base = load_base(conn)
        history = load_history(conn)
    if not base:
        return RedirectResponse("/upload", status_code=303)
    if cls not in ("ok", "err", "warn"):
        cls = "ok"
    return render_index(request, base, history, msg or None, cls)


@app.get("/upload")
def upload_page(request: Request):
    with get_db() as conn:
        base = load_base(conn)
    if base:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "upload.html", {"request": request, "message": None}
    )


@app.post("/upload")
def upload(request: Request, file: UploadFile = File(...)):
    content = file.file.read()
    try:
        rows = parse_csv(content)
    except ValueError as e:
        return templates.TemplateResponse(
            request, "upload.html", {"request": request, "message": str(e)}, status_code=400
        )
    with publish_lock:
        with get_db() as conn:
            if load_base(conn):
                return templates.TemplateResponse(
                    request,
                    "upload.html",
                    {"request": request, "message": "底表已固定，不允许重复上传。"},
                    status_code=409,
                )
            try:
                conn.executemany(
                    "INSERT INTO groups (name, count) VALUES (?, ?)", rows
                )
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                return templates.TemplateResponse(
                    request,
                    "upload.html",
                    {"request": request, "message": f"保存失败：{e}"},
                    status_code=500,
                )
    return RedirectResponse("/", status_code=303)


@app.post("/publish")
def publish(request: Request, selected: list[str] = Form(default=[])):
    with publish_lock:  # 串行化：按实际发布次序核查累计结果
        with get_db() as conn:
            base = load_base(conn)
            history = load_history(conn)
            if not base:
                return RedirectResponse("/upload", status_code=303)

            names = [n for n, _ in base]
            idx = {n: i for i, n in enumerate(names)}
            sel = []
            for s in selected:
                if s in idx and s not in sel:
                    sel.append(s)
            if not sel:
                return redirect_with_msg("请至少勾选一个组。", "err")

            n = len(names)
            total = sum(c for _, c in base)
            new_sum = sum(base[idx[s]][1] for s in sel)

            # 约束：总人数 + 历次已公布合计 + 本次合计
            equations = [([1] * n, total)]
            for h in history:
                row = [0] * n
                for g in h["groups"]:
                    row[idx[g]] = 1
                equations.append((row, h["total"]))
            row = [0] * n
            for s in sel:
                row[idx[s]] = 1
            equations.append((row, new_sum))

            protected = [
                i for i, (_, c) in enumerate(base) if PROTECTED_LO <= c <= PROTECTED_HI
            ]
            forced, inconclusive = forced_protected_groups(n, equations, protected)

            if inconclusive:
                return redirect_with_msg(
                    "求解无定论，本次合计不予发布，发布历史不变。", "warn"
                )
            if forced:
                leaked = "、".join(f"「{names[i]}」" for i in forced)
                return redirect_with_msg(
                    f"拒绝发布：结合总人数与历次已公布合计，保密组 {leaked} "
                    f"的人数将被唯一确定。本次合计未发布，发布历史不变。",
                    "err",
                )
            try:
                conn.execute(
                    "INSERT INTO history (groups, total, created_at) VALUES (?, ?, ?)",
                    (
                        json.dumps(sel, ensure_ascii=False),
                        new_sum,
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                return redirect_with_msg(
                    f"保存失败（{e}），本次合计未发布，发布历史不变。", "err"
                )
            return redirect_with_msg(
                f"已公布：{'、'.join(f'「{s}」' for s in sel)} 的合计 {new_sum} 人。", "ok"
            )
