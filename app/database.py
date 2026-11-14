"""SQLite 持久化：底表（首次上传后固定）与发布历史，重启后沿用。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS groups_t (
    name TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    selected_groups TEXT NOT NULL,
    published_total INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 底表 ----------

    def has_base_table(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
            return row is not None

    def insert_base_table(self, groups: list[tuple[str, int]], total: int) -> None:
        """原子写入底表；若底表已存在则抛出 ``RuntimeError``。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
            if exists is not None:
                raise RuntimeError("底表已固定，不能重复上传")
            conn.execute("INSERT INTO meta (id, total) VALUES (1, ?)", (total,))
            conn.executemany(
                "INSERT INTO groups_t (name, size, position) VALUES (?, ?, ?)",
                [(name, size, pos) for pos, (name, size) in enumerate(groups)],
            )

    def get_base_table(self) -> tuple[list[str], list[int], int] | None:
        """返回 (组名按上传顺序, 各组人数, 总人数)；未上传时返回 None。"""
        with self._connect() as conn:
            meta = conn.execute("SELECT total FROM meta WHERE id = 1").fetchone()
            if meta is None:
                return None
            rows = conn.execute(
                "SELECT name, size FROM groups_t ORDER BY position"
            ).fetchall()
            names = [row["name"] for row in rows]
            sizes = [row["size"] for row in rows]
            return names, sizes, int(meta["total"])

    # ---------- 发布历史 ----------

    def list_publications(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, selected_groups, published_total "
                "FROM publications ORDER BY id"
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "selected_groups": json.loads(row["selected_groups"]),
                    "published_total": row["published_total"],
                }
                for row in rows
            ]

    def insert_publication(self, selected: list[str], published_total: int) -> dict:
        """写入一条发布记录并返回其内容。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO publications (selected_groups, published_total) "
                "VALUES (?, ?)",
                (json.dumps(selected, ensure_ascii=False), published_total),
            )
            pub_id = cur.lastrowid
            row = conn.execute(
                "SELECT id, created_at, selected_groups, published_total "
                "FROM publications WHERE id = ?",
                (pub_id,),
            ).fetchone()
            return {
                "id": row["id"],
                "created_at": row["created_at"],
                "selected_groups": json.loads(row["selected_groups"]),
                "published_total": row["published_total"],
            }
