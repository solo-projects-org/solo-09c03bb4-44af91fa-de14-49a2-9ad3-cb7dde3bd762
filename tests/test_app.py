"""端到端测试：底表固定、保密核查、组合泄露、并发累计、持久化。"""

from __future__ import annotations

import importlib
import os
import tempfile
from urllib.parse import unquote

import pytest

# 每个测试进程使用独立数据库
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="rare_test_"), "test.db")
os.environ["APP_DB_PATH"] = _TMP_DB

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app import main, solver  # noqa: E402


@pytest.fixture()
def client():
    importlib.reload(db)
    importlib.reload(main)
    db.init_db()
    db.reset_all()
    with TestClient(main.app) as c:
        yield c


CSV = (
    "group,count\n"
    "肺动脉高压,12\n"
    "溶酶体贮积症,3\n"
    "遗传性血管性水肿,7\n"
    "肌萎缩侧索硬化,2\n"
    "遗传性共济失调,11\n"
)


def upload(client: TestClient, text: str = CSV) -> tuple[int, dict]:
    r = client.post(
        "/api/upload",
        files={"file": ("sample.csv", text.encode("utf-8"), "text/csv")},
    )
    return r.status_code, r.json()


def test_upload_fixes_base_table_and_sensitive_flag(client):
    status, body = upload(client)
    assert status == 200
    assert body["base_table"]["total"] == 35
    sensitive = {g["name"]: g["sensitive"] for g in body["base_table"]["groups"]}
    assert sensitive["溶酶体贮积症"] is True   # 3 人
    assert sensitive["肌萎缩侧索硬化"] is True   # 2 人
    assert sensitive["肺动脉高压"] is False

    # 底表固定：再次上传被拒
    status2, body2 = upload(client)
    assert status2 == 409
    assert "固定" in body2["detail"]


@pytest.mark.parametrize(
    "bad_csv, fragment",
    [
        ("group,count\nA,1\n", "2~16"),                 # 仅 1 个组
        ("group,count\n" + "".join(f"G{i},1\n" for i in range(17)), "2~16"),  # 17 个组
        ("group,count\nA,1\nB,1001\n", "0~1000"),       # 越界
        ("group,count\nA,1\nA,2\n", "重复"),             # 重复组名
        ("group,count\nA,x\nB,2\n", "不是整数"),          # 非整数
        ("name,n\nA,1\nB,2\n", "group,count"),           # 表头错误
        ("group,count\n", "缺少数据行"),                  # 无数据
    ],
)
def test_csv_validation(client, bad_csv, fragment):
    status, body = upload(client, bad_csv)
    assert status == 400
    assert fragment in body["detail"]


def test_unknown_and_duplicate_group_rejected(client):
    upload(client)
    r = client.post("/api/publish", json={"groups": ["肺动脉高压", "不存在组"]})
    assert r.status_code == 400
    r = client.post("/api/publish", json={"groups": ["肺动脉高压", "肺动脉高压"]})
    assert r.status_code == 400


def test_publish_before_upload_rejected(client):
    r = client.post("/api/publish", json={"groups": ["A"]})
    assert r.status_code == 409


def test_empty_selection_rejected(client):
    upload(client)
    r = client.post("/api/publish", json={"groups": []})
    assert r.status_code == 422  # pydantic 校验失败


def test_bom_csv_and_zero_count_group(client):
    # 带 UTF-8 BOM；含 0 人组（非保密）与 1 人组（保密下界）
    text = "group,count\n甲,0\n乙,1\n丙,5\n".encode("utf-8-sig")
    r = client.post(
        "/api/upload",
        files={"file": ("bom.csv", text, "text/csv")},
    )
    assert r.status_code == 200
    flags = {g["name"]: g["sensitive"] for g in r.json()["base_table"]["groups"]}
    assert flags == {"甲": False, "乙": True, "丙": False}

    # 0 人组不属保密范围：公布甲的单组合计不会因保密规则被拦
    r = client.post("/api/publish", json={"groups": ["甲"]})
    assert r.status_code == 200
    assert r.json()["publication"]["aggregate"] == 0

    # 1 人保密组单组发布必被拒
    r = client.post("/api/publish", json={"groups": ["乙"]})
    assert r.status_code == 422
    assert "乙" in unquote(r.headers["x-exposed-groups"])


def test_no_sensitive_groups_everything_publishable(client):
    text = "group,count\nA,10\nB,20\nC,30\n"
    status, _ = upload(client, text)
    assert status == 200
    r = client.post("/api/publish", json={"groups": ["A"]})
    assert r.status_code == 200  # 无 1~4 人组，任何组合都可发布
    r = client.post("/api/publish", json={"groups": ["A", "B", "C"]})
    assert r.status_code == 200


def test_single_sensitive_group_release_allowed_then_complement_leaks(client):
    upload(client)
    # 只公布非敏感组合（不含任何保密组）：安全，可发布
    r = client.post(
        "/api/publish",
        json={"groups": ["肺动脉高压", "遗传性血管性水肿", "遗传性共济失调"]},
    )
    assert r.status_code == 200
    assert r.json()["publication"]["aggregate"] == 30

    # 直接申请公布一个保密组的单组合计：其人数被唯一确定，拒绝并指出组名
    # （在已有发布的累计约束下，另一保密组同样被钉死，一并指出）
    r = client.post("/api/publish", json={"groups": ["溶酶体贮积症"]})
    assert r.status_code == 422
    assert r.headers["x-disclosure-result"] == "rejected"
    exposed = set(unquote(r.headers["x-exposed-groups"]).split(","))
    assert "溶酶体贮积症" in exposed

    # 拒绝后历史不变
    state = client.get("/api/state").json()
    assert len(state["publications"]) == 1


def test_combination_leakage_demo(client):
    """样例 CSV 的组合泄露演示（差分攻击）：第一次通过，第二次相减即暴露 3 人组。"""
    upload(client)

    # 第一次：肺高压 + 溶酶体 + 血管性水肿 = 22，约束不足，通过
    r1 = client.post(
        "/api/publish",
        json={"groups": ["肺动脉高压", "溶酶体贮积症", "遗传性血管性水肿"]},
    )
    assert r1.status_code == 200
    assert r1.json()["publication"]["aggregate"] == 22

    # 第二次：去掉溶酶体后的两个组 = 19，与第一次联立相减即得溶酶体=3，拒绝
    r2 = client.post(
        "/api/publish",
        json={"groups": ["肺动脉高压", "遗传性血管性水肿"]},
    )
    assert r2.status_code == 422
    exposed = set(unquote(r2.headers["x-exposed-groups"]).split(","))
    assert "溶酶体贮积症" in exposed

    state = client.get("/api/state").json()
    assert len(state["publications"]) == 1  # 第二次未入库


def test_concurrent_requests_serialized_cumulative(client):
    """并发申请按实际发布次序累计：差分攻击对中只有先到者能发布。"""
    import concurrent.futures

    upload(client)
    payloads = [
        # 合计 22：含 3 人保密组的三组合计
        {"groups": ["肺动脉高压", "溶酶体贮积症", "遗传性血管性水肿"]},
        # 合计 19：去掉保密组后的两组合计；若前者已发布，相减即暴露 3 人组
        {"groups": ["肺动脉高压", "遗传性血管性水肿"]},
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(client.post, "/api/publish", json=p) for p in payloads]
        outcomes = [f.result() for f in concurrent.futures.as_completed(futs)]

    statuses = sorted(r.status_code for r in outcomes)
    assert statuses == [200, 422]  # 一个发布、一个被拒，不会双双通过

    state = client.get("/api/state").json()
    assert len(state["publications"]) == 1


def test_inconclusive_blocks_publication(monkeypatch, client):
    upload(client)

    def boom(*args, **kwargs):
        raise solver.InconclusiveError("模拟求解器超时")

    monkeypatch.setattr(main, "check_disclosure", boom)
    r = client.post("/api/publish", json={"groups": ["肺动脉高压"]})
    assert r.status_code == 409
    assert r.headers["x-disclosure-result"] == "inconclusive"

    state = client.get("/api/state").json()
    assert state["publications"] == []


def test_persistence_survives_restart(client):
    upload(client)
    client.post("/api/publish", json={"groups": ["肺动脉高压"]})

    # 模拟重启：重新加载模块（数据库文件不变）
    importlib.reload(db)
    importlib.reload(main)
    with TestClient(main.app) as c2:
        state = c2.get("/api/state").json()
        assert state["initialized"] is True
        assert state["base_table"]["total"] == 35
        assert len(state["publications"]) == 1
        assert state["publications"][0]["aggregate"] == 12
        # 底表依旧固定
        r = c2.post(
            "/api/upload",
            files={"file": ("x.csv", CSV.encode(), "text/csv")},
        )
        assert r.status_code == 409


def test_solver_unit_pin_and_free():
    """直接验证 MILP 核查：约束足够时识别泄露，不足时放行。"""
    names = ["A", "B", "C"]
    counts = [2, 5, 8]  # 仅 A 保密
    total = 15

    # 已发布 B+C=13（等价于 A=2 的补集），再发 A 的组合时 A 被钉死
    pubs = [{"groups": ["B", "C"], "aggregate": 13}]
    exposed = solver.check_disclosure(names, counts, pubs, ["A"], 2)
    assert exposed == ["A"]

    # 仅总人数 + 本次 A+B=7：约束不足，A 可取 0..7 等多个值，安全
    exposed = solver.check_disclosure(names, counts, [], ["A", "B"], 7)
    assert exposed == []

    # 零人组不属于保密组（规则只保护 1~4 人），即使被唯一确定也不拦截
    names2 = ["Z", "B", "C"]
    counts2 = [0, 5, 10]
    pubs2 = [{"groups": ["B", "C"], "aggregate": 15}]
    exposed = solver.check_disclosure(names2, counts2, pubs2, ["Z"], 0)
    assert exposed == []
