"""群宠物养成插件 - SQLite 数据持久层。

不依赖 AstrBot 运行时，可独立进行单元测试。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS pets (
    owner_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    species    TEXT NOT NULL,
    level      INTEGER NOT NULL DEFAULT 1,
    exp        INTEGER NOT NULL DEFAULT 0,
    hunger     INTEGER NOT NULL DEFAULT 80,
    mood       INTEGER NOT NULL DEFAULT 80,
    coins      INTEGER NOT NULL DEFAULT 30,
    group_id   TEXT NOT NULL DEFAULT '',
    cooldowns  TEXT NOT NULL DEFAULT '{}',
    starve_since REAL,
    adopt_time REAL NOT NULL,
    adventures INTEGER NOT NULL DEFAULT 0
);
"""

PET_FIELDS = (
    "owner_id", "name", "species", "level", "exp", "hunger", "mood", "coins",
    "group_id", "cooldowns", "starve_since", "adopt_time", "adventures",
)


def clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(value)))


class PetStore:
    def __init__(self, db_path: Path | str):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ---------- 行转换 ----------

    @staticmethod
    def _to_pet(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        pet = dict(row)
        try:
            pet["cooldowns"] = json.loads(pet.get("cooldowns") or "{}")
        except (TypeError, ValueError):
            pet["cooldowns"] = {}
        if not isinstance(pet["cooldowns"], dict):
            pet["cooldowns"] = {}
        return pet

    # ---------- 基础 CRUD ----------

    def get_pet(self, owner_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM pets WHERE owner_id=?", (str(owner_id),)
        ).fetchone()
        return self._to_pet(row)

    def create_pet(
        self, owner_id: str, name: str, species: str, group_id: str = ""
    ) -> dict[str, Any]:
        now = time.time()
        self._db.execute(
            "INSERT INTO pets (owner_id, name, species, group_id, adopt_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(owner_id), name, species, str(group_id or ""), now),
        )
        self._db.commit()
        return self.get_pet(owner_id)

    def save_pet(self, pet: dict[str, Any]) -> None:
        self._db.execute(
            "UPDATE pets SET name=?, species=?, level=?, exp=?, hunger=?, mood=?, "
            "coins=?, group_id=?, cooldowns=?, starve_since=?, adventures=? "
            "WHERE owner_id=?",
            (
                pet["name"],
                pet["species"],
                int(pet["level"]),
                int(pet["exp"]),
                int(pet["hunger"]),
                int(pet["mood"]),
                int(pet["coins"]),
                str(pet.get("group_id", "")),
                json.dumps(pet.get("cooldowns", {}), ensure_ascii=False),
                pet.get("starve_since"),
                int(pet.get("adventures", 0)),
                str(pet["owner_id"]),
            ),
        )
        self._db.commit()

    def delete_pet(self, owner_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM pets WHERE owner_id=?", (str(owner_id),)
        )
        self._db.commit()
        return cur.rowcount > 0

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM pets").fetchone()[0])

    # ---------- 排行 / 遍历 ----------

    def top_pets(self, group_id: Optional[str] = None, limit: int = 10) -> list[dict]:
        if group_id:
            rows = self._db.execute(
                "SELECT * FROM pets WHERE group_id=? "
                "ORDER BY level DESC, exp DESC, adopt_time ASC LIMIT ?",
                (str(group_id), int(limit)),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM pets ORDER BY level DESC, exp DESC, adopt_time ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [p for p in (self._to_pet(r) for r in rows) if p]

    def all_pets(self) -> list[dict]:
        rows = self._db.execute("SELECT * FROM pets").fetchall()
        return [p for p in (self._to_pet(r) for r in rows) if p]
