"""SQLite 持久化层：底表（首次上传后固定）与发布历史，重启后沿用。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("APP_DB_PATH", "/data/rare.db")

# 串行化“核查 + 落库”，保证并发申请按实际发布次序累计核查
publish_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS groups_t (
                id         INTEGER PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                count      INTEGER NOT NULL,
                sensitive  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                groups_j   TEXT NOT NULL,
                aggregate  INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def has_base_table() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM groups_t LIMIT 1").fetchone()
        return row is not None


def get_base_table() -> dict | None:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, count, sensitive FROM groups_t ORDER BY id"
        ).fetchall()
        if not rows:
            return None
        total = conn.execute(
            "SELECT value FROM meta WHERE key = 'total'"
        ).fetchone()
        return {
            "groups": [
                {
                    "name": r["name"],
                    "count": r["count"],
                    "sensitive": bool(r["sensitive"]),
                }
                for r in rows
            ],
            "total": int(total["value"]) if total else sum(r["count"] for r in rows),
        }


def insert_base_table(group_names: list[str], counts: list[int]) -> None:
    """写入底表。仅允许在底表不存在时调用（调用方先检查锁）。"""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('total', ?), ('uploaded_at', ?)",
                (str(sum(counts)), now),
            )
            conn.executemany(
                "INSERT INTO groups_t(name, count, sensitive) VALUES(?, ?, ?)",
                [
                    (name, count, 1 if 1 <= count <= 4 else 0)
                    for name, count in zip(group_names, counts)
                ],
            )
        except sqlite3.Error:
            conn.rollback()
            raise


def list_publications() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, groups_j, aggregate, created_at "
            "FROM publications ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "groups": json.loads(r["groups_j"]),
                "aggregate": r["aggregate"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def insert_publication(group_names: list[str], aggregate: int) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO publications(groups_j, aggregate, created_at) "
            "VALUES(?, ?, ?)",
            (json.dumps(group_names, ensure_ascii=False), aggregate, now),
        )
        pub_id = cur.lastrowid
    return {
        "id": pub_id,
        "groups": list(group_names),
        "aggregate": aggregate,
        "created_at": now,
    }


def reset_all() -> None:
    """清空底表与历史（仅用于演示/测试）。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM publications")
        conn.execute("DELETE FROM groups_t")
        conn.execute("DELETE FROM meta")
