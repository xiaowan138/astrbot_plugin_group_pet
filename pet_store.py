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
    owner_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    species       TEXT NOT NULL,
    level         INTEGER NOT NULL DEFAULT 1,
    exp           INTEGER NOT NULL DEFAULT 0,
    hunger        REAL NOT NULL DEFAULT 80,
    mood          REAL NOT NULL DEFAULT 80,
    coins         INTEGER NOT NULL DEFAULT 30,
    group_id      TEXT NOT NULL DEFAULT '',
    cooldowns     TEXT NOT NULL DEFAULT '{}',
    items         TEXT NOT NULL DEFAULT '{}',
    starve_since  REAL,
    starve_warned INTEGER NOT NULL DEFAULT 0,
    umo           TEXT NOT NULL DEFAULT '',
    adopt_time    REAL NOT NULL,
    adventures    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pets_group ON pets(group_id);

CREATE TABLE IF NOT EXISTS profiles (
    owner_id     TEXT PRIMARY KEY,
    seen_species TEXT NOT NULL DEFAULT '[]',
    sign_date    TEXT NOT NULL DEFAULT '',
    sign_streak  INTEGER NOT NULL DEFAULT 0,
    daily        TEXT NOT NULL DEFAULT '{}',
    stats        TEXT NOT NULL DEFAULT '{}',
    achievements TEXT NOT NULL DEFAULT '[]'
);
"""

# 旧版本数据库缺失的列（CREATE TABLE IF NOT EXISTS 不会补列）
PET_MIGRATIONS: dict[str, str] = {
    "items": "TEXT NOT NULL DEFAULT '{}'",
    "starve_warned": "INTEGER NOT NULL DEFAULT 0",
    "umo": "TEXT NOT NULL DEFAULT ''",
}


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    """保留小数，避免后台衰减的 0.5 倍率被取整吃掉。"""
    return max(low, min(high, value))


def _json_or(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    if isinstance(default, dict):
        return value if isinstance(value, dict) else default
    if isinstance(default, list):
        return value if isinstance(value, list) else default
    return value


def _default_profile(owner_id: str) -> dict[str, Any]:
    return {
        "owner_id": str(owner_id),
        "seen_species": [],
        "sign_date": "",
        "sign_streak": 0,
        "daily": {},
        "stats": {},
        "achievements": [],
    }


class PetStore:
    def __init__(self, db_path: Path | str):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(pets)")}
        for name, decl in PET_MIGRATIONS.items():
            if name not in cols:
                self._db.execute(f"ALTER TABLE pets ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self._db.close()

    # ---------- 行转换 ----------

    @staticmethod
    def _to_pet(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        pet = dict(row)
        pet["cooldowns"] = _json_or(pet.get("cooldowns"), {})
        pet["items"] = _json_or(pet.get("items"), {})
        pet["starve_warned"] = int(pet.get("starve_warned") or 0)
        pet["umo"] = pet.get("umo") or ""
        pet["adventures"] = int(pet.get("adventures") or 0)
        return pet

    @staticmethod
    def _pack_pet(pet: dict[str, Any]) -> tuple:
        return (
            pet["name"],
            pet["species"],
            int(pet["level"]),
            int(pet["exp"]),
            float(pet["hunger"]),
            float(pet["mood"]),
            int(pet["coins"]),
            str(pet.get("group_id", "")),
            json.dumps(pet.get("cooldowns", {}), ensure_ascii=False),
            json.dumps(pet.get("items", {}), ensure_ascii=False),
            pet.get("starve_since"),
            int(pet.get("starve_warned", 0)),
            str(pet.get("umo", "")),
            int(pet.get("adventures", 0)),
            str(pet["owner_id"]),
        )

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

    _SAVE_SQL = (
        "UPDATE pets SET name=?, species=?, level=?, exp=?, hunger=?, mood=?, "
        "coins=?, group_id=?, cooldowns=?, items=?, starve_since=?, starve_warned=?, "
        "umo=?, adventures=? WHERE owner_id=?"
    )

    def save_pet(self, pet: dict[str, Any]) -> None:
        self._db.execute(self._SAVE_SQL, self._pack_pet(pet))
        self._db.commit()

    def save_pets(self, pets: list[dict[str, Any]]) -> None:
        """批量保存（衰减任务用，避免每只宠物一次 commit）。"""
        if not pets:
            return
        self._db.executemany(self._SAVE_SQL, [self._pack_pet(p) for p in pets])
        self._db.commit()

    def delete_pet(self, owner_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM pets WHERE owner_id=?", (str(owner_id),)
        )
        self._db.commit()
        return cur.rowcount > 0

    def delete_pets(self, owner_ids: list[str]) -> None:
        if not owner_ids:
            return
        self._db.executemany(
            "DELETE FROM pets WHERE owner_id=?", [(str(i),) for i in owner_ids]
        )
        self._db.commit()

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM pets").fetchone()[0])

    # ---------- 用户档案（图鉴/签到/任务/成就） ----------

    def get_profile(self, owner_id: str) -> dict[str, Any]:
        owner = str(owner_id)
        row = self._db.execute(
            "SELECT * FROM profiles WHERE owner_id=?", (owner,)
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT OR IGNORE INTO profiles (owner_id) VALUES (?)", (owner,)
            )
            self._db.commit()
            return _default_profile(owner)
        profile = dict(row)
        profile["owner_id"] = owner
        profile["seen_species"] = _json_or(profile.get("seen_species"), [])
        profile["sign_date"] = profile.get("sign_date") or ""
        profile["sign_streak"] = int(profile.get("sign_streak") or 0)
        profile["daily"] = _json_or(profile.get("daily"), {})
        profile["stats"] = _json_or(profile.get("stats"), {})
        profile["achievements"] = _json_or(profile.get("achievements"), [])
        return profile

    def save_profile(self, profile: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO profiles "
            "(owner_id, seen_species, sign_date, sign_streak, daily, stats, achievements) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(profile["owner_id"]),
                json.dumps(list(profile.get("seen_species", [])), ensure_ascii=False),
                str(profile.get("sign_date", "")),
                int(profile.get("sign_streak", 0)),
                json.dumps(profile.get("daily", {}), ensure_ascii=False),
                json.dumps(profile.get("stats", {}), ensure_ascii=False),
                json.dumps(list(profile.get("achievements", [])), ensure_ascii=False),
            ),
        )
        self._db.commit()

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
