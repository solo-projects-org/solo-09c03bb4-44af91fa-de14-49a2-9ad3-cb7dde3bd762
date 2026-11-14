"""端到端 API 测试：上传、发布、拒绝、并发串行化与重启持久化。"""

from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient

CSV = b"group,count\nA,2\nB,10\nC,20\nD,68\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试使用独立 SQLite 文件，并重新加载 app 以重置全局锁与连接。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))
    import app.main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c, main_module, db_path


def _upload(c):
    return c.post(
        "/api/upload",
        files={"file": ("groups.csv", CSV, "text/csv")},
    )


def test_state_starts_empty(client):
    c, _, _ = client
    state = c.get("/api/state").json()
    assert state["initialized"] is False
    assert state["history"] == []


def test_upload_fixes_base_table(client):
    c, _, _ = client
    res = _upload(c)
    assert res.status_code == 200
    state = res.json()
    assert state["initialized"] is True
    assert state["total"] == 100
    by_name = {g["name"]: g for g in state["groups"]}
    assert by_name["A"] == {"name": "A", "size": 2, "secret": True}
    assert by_name["D"]["secret"] is False


def test_duplicate_upload_rejected(client):
    c, _, _ = client
    assert _upload(c).status_code == 200
    res = _upload(c)
    assert res.status_code == 409
    # 底表未被覆盖。
    assert c.get("/api/state").json()["total"] == 100


def test_bad_csv_rejected(client):
    c, _, _ = client
    bad = b"group,count\nA,99999\nB,1\n"  # 人数超出 0~1000
    res = c.post("/api/upload", files={"file": ("bad.csv", bad, "text/csv")})
    assert res.status_code == 400
    assert c.get("/api/state").json()["initialized"] is False


def test_publish_before_upload_rejected(client):
    c, _, _ = client
    res = c.post("/api/publish", json={"groups": ["A"]})
    assert res.status_code == 409


def test_safe_publication_is_recorded(client):
    c, _, _ = client
    _upload(c)
    res = c.post("/api/publish", json={"groups": ["B", "C"]})
    assert res.status_code == 200
    body = res.json()
    assert body["accepted"] is True
    assert body["publication"]["published_total"] == 30
    history = c.get("/api/state").json()["history"]
    assert len(history) == 1 and history[0]["selected_groups"] == ["B", "C"]


def test_leak_rejection_names_group_and_keeps_history(client):
    c, _, _ = client
    _upload(c)
    # 直接发布保密组 A：被拒绝并指出组名。
    res = c.post("/api/publish", json={"groups": ["A"]})
    assert res.status_code == 422
    assert res.json()["leaked_groups"] == ["A"]
    assert c.get("/api/state").json()["history"] == []


def test_combination_leak_demo_flow(client):
    """样例 CSV 的组合泄露演示：①②可发布，③被拒且历史不变。"""
    c, _, _ = client
    _upload(c)

    r1 = c.post("/api/publish", json={"groups": ["B", "C"]})       # =30
    r2 = c.post("/api/publish", json={"groups": ["A", "C"]})       # =22
    assert r1.status_code == 200 and r2.status_code == 200

    r3 = c.post("/api/publish", json={"groups": ["A", "B"]})       # =12，联立泄露 A
    assert r3.status_code == 422
    assert r3.json()["leaked_groups"] == ["A"]

    history = c.get("/api/state").json()["history"]
    assert len(history) == 2  # 被拒的第三次没有写入


def test_concurrent_requests_serialized_no_collective_leak(client):
    """三个申请并发提交：无论实际次序如何，恰好两个入库，泄露申请被拒。"""
    c, _, _ = client
    _upload(c)
    selections = [["B", "C"], ["A", "C"], ["A", "B"]]
    outcomes: list[tuple[int, list[str]]] = []
    barrier = threading.Barrier(len(selections))

    def worker(sel):
        barrier.wait()
        res = c.post("/api/publish", json={"groups": sel})
        outcomes.append((res.status_code, sel))

    threads = [threading.Thread(target=worker, args=(s,)) for s in selections]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 200, 422]

    history = c.get("/api/state").json()["history"]
    assert len(history) == 2

    # 找出实际被拒的申请，独立复核：它与已发布的两条历史联立确实会泄密，
    # 因而无论三个线程的实际取得锁次序如何，拒绝都是正确行为。
    rejected = [sel for code, sel in outcomes if code == 422]
    assert len(rejected) == 1
    import app.checker as checker
    names = ["A", "B", "C", "D"]
    result = checker.check_publication(
        group_names=names,
        secret_flags=[True, False, False, False],
        total=100,
        history_selections=[h["selected_groups"] for h in history],
        history_totals=[h["published_total"] for h in history],
        current_selection=rejected[0],
        current_total=sum({"A": 2, "B": 10, "C": 20, "D": 68}[g] for g in rejected[0]),
    )
    assert "A" in result.leaked_groups


def test_state_persists_across_restart(client, monkeypatch):
    c, main_module, db_path = client
    _upload(c)
    c.post("/api/publish", json={"groups": ["B", "C"]})

    # 模拟重启：重新加载应用模块，沿用同一个 SQLite 文件。
    importlib.reload(main_module)
    new_client = TestClient(main_module.app)
    state = new_client.get("/api/state").json()
    assert state["initialized"] is True
    assert state["total"] == 100
    assert len(state["history"]) == 1
    assert state["history"][0]["published_total"] == 30

    # 重启后底表依旧固定。
    res = new_client.post(
        "/api/upload", files={"file": ("again.csv", CSV, "text/csv")}
    )
    assert res.status_code == 409
