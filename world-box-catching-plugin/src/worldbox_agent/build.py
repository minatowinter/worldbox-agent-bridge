"""Load a WorldBox world snapshot and normalise it into a queryable index.

Two payload encodings exist in 0.51.2:

* ``map.wbax`` — plain UTF-8 JSON (autosaves). Starts with ``{"saveVersion":``.
* ``map.wbox`` — the same JSON, zlib-deflated (manual saves).

``map_stats.s3db`` is SQLite and holds the parts of the world the JSON does not:
per-entity YEARLY time series and the world event log. History is what lets an
agent-led nation "remember" -- so it is treated as first-class, not optional.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import SCHEMA_VERSION as SCHEMA_VERSION_STR
from .paths import SaveFolder


# --------------------------------------------------------------------------
# payload decoding
# --------------------------------------------------------------------------

class SaveFormatError(RuntimeError):
    pass


def decode_payload(path: Path) -> dict[str, Any]:
    """Read ``map.wbax`` / ``map.wbox`` and return the raw world dict."""
    raw = path.read_bytes()
    if not raw:
        raise SaveFormatError(f"{path.name} is empty")

    if raw[:1] == b"{":
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SaveFormatError(f"{path.name}: invalid JSON payload: {exc}") from exc

    # zlib stream (0x78 0x01 / 0x9c / 0xda) -- the .wbox container.
    for wbits in (15, -15, 47):
        try:
            text = zlib.decompress(raw, wbits).decode("utf-8-sig")
        except (zlib.error, UnicodeDecodeError):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SaveFormatError(f"{path.name}: invalid JSON after inflate: {exc}") from exc

    raise SaveFormatError(
        f"{path.name}: unrecognised container "
        f"(magic {raw[:4].hex()}); expected JSON or zlib"
    )


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: Any, default: int = -1) -> int:
    return value if isinstance(value, int) else default


def _as_float(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


# Unit asset ids that are vehicles, not people. Counting them as population
# would inflate a coastal nation's headcount and make the same nation report two
# different populations in different documents.
_BOAT_HINTS = ("boat", "ship", "raft", "canoe", "galley")


def _is_boat_asset(asset_id: str) -> bool:
    lowered = asset_id.lower()
    return any(hint in lowered for hint in _BOAT_HINTS)


# --------------------------------------------------------------------------
# history (map_stats.s3db)
# --------------------------------------------------------------------------

# Bucketed tables the game maintains; 1 = every year, 100 = every century.
HISTORY_TABLES = ("KingdomYearly1", "KingdomYearly10", "KingdomYearly100")

# Columns worth handing to an agent, in the order a leader would want them.
_HISTORY_FIELDS = (
    "timestamp", "population", "adults", "children", "army", "boats",
    "sick", "hungry", "starving", "happy", "kills", "deaths", "births",
    "territory", "buildings", "homeless", "housed", "food", "families",
    "males", "females", "cities", "renown", "money",
)


def _unlink_quiet(path: Optional[Path]) -> None:
    try:
        if path is not None:
            os.unlink(path)
    except OSError:
        pass


def _copy_db_to_temp(src: Path) -> Path:
    """把 ``map_stats.s3db`` 整个复制成一个私有临时文件，返回它的路径。

    复制而不是直接打开，是为了**手上一个游戏文件的句柄都不留**：游戏存档时会先
    删除再重写 ``map_stats.s3db``，任何未共享删除权限的句柄都会让它失败。

    临时目录按序尝试（系统临时目录 → 当前目录），因为沙箱/权限可能不允许前者。
    """
    last: Optional[BaseException] = None
    for directory in (Path(tempfile.gettempdir()), Path.cwd()):
        try:
            fd, name = tempfile.mkstemp(prefix="worldbox-stats-", suffix=".s3db",
                                        dir=str(directory))
        except OSError as exc:
            last = exc
            continue
        dst = Path(name)
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
                shutil.copyfileobj(source, out, 1024 * 256)
            return dst
        except BaseException as exc:  # noqa: BLE001 - 换个目录再试
            _unlink_quiet(dst)
            last = exc
            continue
    raise OSError(f"无法创建 map_stats.s3db 的临时副本: {last}")


@dataclass
class StatsDatabase:
    """Read-only access to ``map_stats.s3db`` (tolerates a missing/locked file).

    The connection is deliberately opened with ``check_same_thread=False``: the
    HTTP server builds the snapshot on its main thread but answers queries from
    worker threads, and sqlite3 otherwise refuses to share a connection across
    threads.  A lock serialises access (sqlite's own default mode is not
    guaranteed to be serialised on every build).

    **打开的是副本，不是游戏那一份。** 见 :meth:`open` 里的说明——直接持有游戏
    文件的句柄会让游戏下一次存档失败。
    """

    path: Optional[Path]
    available: bool = False
    error: str = ""
    tables: tuple[str, ...] = ()
    _conn: Optional[sqlite3.Connection] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _temp_path: Optional[Path] = None
    copied: bool = False
    note: str = ""

    @classmethod
    def open(cls, path: Optional[Path]) -> "StatsDatabase":
        db = cls(path=path)
        if path is None or not path.is_file():
            db.error = "no map_stats.s3db in this save folder"
            return db

        # 先复制成自己的临时副本，再打开副本。
        #
        # 为什么非得这样（实测，不是理论）：SQLite 打开文件时不共享"删除"权限，
        # 而 WorldBox 存档的第一步就是 File.Delete(<存档目录>\map_stats.s3db)
        # （DBManager.saveToPath）。所以只要我们还开着游戏那个文件，游戏这次存档
        # 必然失败：
        #   System.IO.IOException: The process cannot access the file
        #   '...\saves\save3\map_stats.s3db' because it is being used by another process.
        # 读副本后手上没有任何游戏文件的句柄，游戏随时都能存；顺带还避免了
        # "读到写了一半的库"（immutable=1 原本想解决的正是半个问题）。
        #
        # 副本可能因为游戏正在写而复制失败/复制到半份 → 重试几次。
        last_error = ""
        for attempt in range(3):
            tmp: Optional[Path] = None
            try:
                tmp = _copy_db_to_temp(path)
                conn = sqlite3.connect(f"file:{tmp.as_posix()}?mode=ro", uri=True,
                                       check_same_thread=False)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            except (OSError, sqlite3.Error) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if tmp is not None:
                    _unlink_quiet(tmp)
                time.sleep(0.2 * (attempt + 1))
                continue
            db._temp_path = tmp
            db._conn = conn
            db.copied = True
            db.tables = tuple(r[0] for r in rows)
            db.available = True
            return db

        # 连副本都做不出来（临时目录不可写？）→ 退回直接打开，但如实说明代价。
        try:
            uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
            db._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            db._conn.row_factory = sqlite3.Row
            rows = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            db.tables = tuple(r[0] for r in rows)
            db.available = True
            db.note = ("读不到临时目录，只能直接打开游戏那份库："
                       "工具运行期间游戏可能无法保存这个世界")
        except sqlite3.Error as exc:
            db.error = f"{type(exc).__name__}: {exc}"
            db._conn = None
        return db

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                finally:
                    self._conn = None
            if self._temp_path is not None:
                _unlink_quiet(self._temp_path)
                self._temp_path = None

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            if not self.available or self._conn is None:
                return []
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return []

    def _has(self, table: str) -> bool:
        return self.available and table in self.tables

    def _columns(self, table: str) -> list[str]:
        """Column names of *table*.

        NOTE: the connection uses ``sqlite3.Row``, so ``PRAGMA table_info`` rows
        must be read by *index*: index 0 is ``cid`` and index 1 is the name.
        Reading ``row[0]`` here silently yields the integer cid, which then
        matches no field and makes every history query return nothing.
        """
        return [r[1] for r in self._query(f'PRAGMA table_info("{table}")')]

    def history(self, table: str, entity_id: int, limit: int = 40) -> list[dict[str, Any]]:
        """Newest *limit* rows of ``table`` for one entity, oldest-first."""
        if not self._has(table):
            return []
        cols = self._columns(table)
        wanted = [c for c in _HISTORY_FIELDS if c in cols]
        if not wanted:
            return []
        sel = ", ".join(f'"{c}"' for c in wanted)
        rows = self._query(
            f'SELECT {sel} FROM "{table}" WHERE id = ? '
            f'ORDER BY timestamp DESC LIMIT ?',
            (entity_id, limit),
        )
        out = []
        for row in rows:
            rec = {c: row[c] for c in wanted if row[c] is not None}
            if rec:
                out.append(rec)
        out.reverse()
        return out

    def world_history(self, table: str, limit: int = 60) -> list[dict[str, Any]]:
        """World-wide series straight from ``WorldYearly*`` (no per-id filter)."""
        if not self._has(table):
            return []
        cols = self._columns(table)
        wanted = [c for c in (
            "timestamp", "kingdoms", "cities", "population_civ",
            "population_beasts", "wars", "wars_started", "peaces_made",
            "kingdoms_created", "kingdoms_destroyed", "cities_conquered",
            "cities_rebelled", "deaths_total", "houses",
        ) if c in cols]
        if not wanted:
            return []
        sel = ", ".join(f'"{c}"' for c in wanted)
        rows = self._query(
            f'SELECT {sel} FROM "{table}" ORDER BY timestamp DESC LIMIT ?',
            (limit,),
        )
        out = [{c: r[c] for c in wanted if r[c] is not None} for r in rows]
        out.reverse()
        return out

    def log(self, limit: int = 200, kingdom_id: Optional[int] = None,
            since_timestamp: Optional[int] = None) -> list[dict[str, Any]]:
        """World event log, newest last. Optionally scoped to one kingdom."""
        if not self._has("WorldLogMessage"):
            return []
        sql = ("SELECT asset_id, timestamp, special1, special2, special3, "
               "unit_id, kingdom_id, x, y FROM WorldLogMessage")
        clauses, params = [], []
        if kingdom_id is not None:
            clauses.append("kingdom_id = ?")
            params.append(kingdom_id)
        if since_timestamp is not None:
            clauses.append("timestamp >= ?")
            params.append(since_timestamp)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        out = [dict(r) for r in self._query(sql, tuple(params))]
        out.reverse()
        return out

    def all_kingdom_series(self, table: str = "KingdomYearly100") -> dict[int, list[dict]]:
        """Every kingdom's series in one pass -- used for trend/rank context."""
        if not self._has(table):
            return {}
        cols = self._columns(table)
        wanted = [c for c in _HISTORY_FIELDS if c in cols]
        if not wanted:
            return {}
        sel = ", ".join(["id"] + [f'"{c}"' for c in wanted])
        rows = self._query(f'SELECT {sel} FROM "{table}" ORDER BY id, timestamp')
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(int(r["id"]), []).append(
                {c: r[c] for c in wanted if r[c] is not None}
            )
        return out


# --------------------------------------------------------------------------
# normalised world
# --------------------------------------------------------------------------

@dataclass
class WorldSnapshot:
    """A normalised, queryable view of one world moment."""

    save_version: int = 0
    width: int = 0
    height: int = 0
    timestamp: float = 0.0
    source: str = ""
    source_kind: str = ""
    generated_at: float = 0.0

    world_meta: dict[str, Any] = field(default_factory=dict)
    world_stats: dict[str, Any] = field(default_factory=dict)
    world_laws: list[str] = field(default_factory=list)

    kingdoms: dict[int, dict[str, Any]] = field(default_factory=dict)
    cities: dict[int, dict[str, Any]] = field(default_factory=dict)
    armies: dict[int, dict[str, Any]] = field(default_factory=dict)
    wars: dict[int, dict[str, Any]] = field(default_factory=dict)
    alliances: dict[int, dict[str, Any]] = field(default_factory=dict)
    relations: dict[str, dict[str, Any]] = field(default_factory=dict)
    cultures: dict[int, dict[str, Any]] = field(default_factory=dict)
    languages: dict[int, dict[str, Any]] = field(default_factory=dict)
    religions: dict[int, dict[str, Any]] = field(default_factory=dict)
    subspecies: dict[int, dict[str, Any]] = field(default_factory=dict)
    clans: dict[int, dict[str, Any]] = field(default_factory=dict)
    families: dict[int, dict[str, Any]] = field(default_factory=dict)
    plots: dict[int, dict[str, Any]] = field(default_factory=dict)
    books: dict[int, dict[str, Any]] = field(default_factory=dict)
    actors: dict[int, dict[str, Any]] = field(default_factory=dict)

    stats: Optional[StatsDatabase] = None

    # derived
    kingdom_cities: dict[int, list[int]] = field(default_factory=dict)
    # kingdom_units holds CIVILIANS only (boats excluded): a "population" that
    # silently counted warships would disagree with the per-nation view, and
    # two different population numbers for the same nation is exactly the kind
    # of ambiguity that makes an agent distrust the whole feed.
    kingdom_units: dict[int, list[int]] = field(default_factory=dict)
    kingdom_boats: dict[int, int] = field(default_factory=dict)
    kingdom_all_units: dict[int, int] = field(default_factory=dict)
    kingdom_armies: dict[int, list[int]] = field(default_factory=dict)
    city_units: dict[int, list[int]] = field(default_factory=dict)
    city_all_units: dict[int, int] = field(default_factory=dict)
    city_boats: dict[int, int] = field(default_factory=dict)
    city_centers: dict[int, list[int]] = field(default_factory=dict)
    city_building_index: dict[int, list[int]] = field(default_factory=dict)
    city_housing: dict[int, dict[str, int]] = field(default_factory=dict)
    army_size_map: dict[int, int] = field(default_factory=dict)
    kingdom_territory: dict[int, int] = field(default_factory=dict)
    kingdom_buildings: dict[int, int] = field(default_factory=dict)
    kingdom_wars: dict[int, list[int]] = field(default_factory=dict)
    kingdom_log: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    world_log: list[dict[str, Any]] = field(default_factory=list)
    year: int = 0
    coord_bounds: list[int] = field(default_factory=list)  # [x_min, y_min, x_max, y_max]

    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return _as_str(self.world_meta.get("name"), "Unnamed world")

    def kingdom(self, kingdom_id: int) -> Optional[dict[str, Any]]:
        return self.kingdoms.get(kingdom_id)

    def city(self, city_id: int) -> Optional[dict[str, Any]]:
        return self.cities.get(city_id)

    def actor(self, actor_id: int) -> Optional[dict[str, Any]]:
        return self.actors.get(actor_id)

    def actor_name(self, actor_id: Any) -> str:
        actor = self.actors.get(actor_id) if isinstance(actor_id, int) else None
        return _as_str(actor.get("name")) if actor else ""

    def live_kingdoms(self) -> list[dict[str, Any]]:
        """Kingdoms with at least one city, most powerful first."""
        out = [k for k in self.kingdoms.values() if self.kingdom_cities.get(k["id"])]
        out.sort(key=lambda k: (
            _as_int(k.get("renown"), 0),
            self.population_of(k["id"]),
        ), reverse=True)
        return out

    # -- aggregates ----------------------------------------------------
    def population_of(self, kingdom_id: int) -> int:
        """常住人口（不含船只）。这是对外统一口径的「人口」。"""
        return len(self.kingdom_units.get(kingdom_id, ()))

    def total_population_of(self, kingdom_id: int) -> int:
        """含船只的存档条目总数。只有需要和原始存档条目数对齐时才用它。"""
        return self.kingdom_all_units.get(kingdom_id, 0)

    def boats_of(self, kingdom_id: int) -> int:
        return self.kingdom_boats.get(kingdom_id, 0)

    def army_size_of(self, kingdom_id: int) -> int:
        """在编军人数量 = 本国军队所含兵力之和（由单位上的 ``army`` 字段统计）。"""
        return sum(self.army_size_map.get(aid, 0)
                   for aid in self.kingdom_armies.get(kingdom_id, ()))

    def city_count_of(self, kingdom_id: int) -> int:
        return len(self.kingdom_cities.get(kingdom_id, ()))

    def renown_rank(self, kingdom_id: int) -> int:
        order = {k["id"]: i + 1 for i, k in enumerate(self.live_kingdoms())}
        return order.get(kingdom_id, 0)

    def population_rank(self, kingdom_id: int) -> int:
        ranked = sorted(self.kingdoms.values(),
                        key=lambda k: self.population_of(k["id"]), reverse=True)
        order = {k["id"]: i + 1 for i, k in enumerate(ranked)}
        return order.get(kingdom_id, 0)

    # -- diplomacy -----------------------------------------------------
    def relation_between(self, a: int, b: int) -> Optional[dict[str, Any]]:
        for key in (f"{a}_{b}", f"{b}_{a}"):
            if key in self.relations:
                return self.relations[key]
        return None

    def wars_of(self, kingdom_id: int) -> list[dict[str, Any]]:
        return [self.wars[w] for w in self.kingdom_wars.get(kingdom_id, ())
                if w in self.wars]

    def enemy_ids_of(self, kingdom_id: int) -> list[int]:
        enemies: list[int] = []
        for war in self.wars_of(kingdom_id):
            for other in _as_list(war.get("list_attackers")) + _as_list(war.get("list_defenders")):
                if isinstance(other, int) and other != kingdom_id and other not in enemies:
                    enemies.append(other)
        return enemies

    def ally_ids_of(self, kingdom_id: int) -> list[int]:
        allies: list[int] = []
        for alliance in self.alliances.values():
            members = [m for m in _as_list(alliance.get("list_kingdoms")) if isinstance(m, int)]
            if kingdom_id in members:
                allies.extend(m for m in members if m != kingdom_id)
        return allies

    def history_of(self, kingdom_id: int, table: str = "KingdomYearly1",
                   limit: int = 40) -> list[dict[str, Any]]:
        if self.stats is None:
            return []
        return self.stats.history(table, kingdom_id, limit=limit)

    # -- serialisation -------------------------------------------------
    def to_snapshot_dict(self) -> dict[str, Any]:
        """The canonical machine-readable snapshot (raw-ish, complete)."""
        return {
            "schema_version": SCHEMA_VERSION_STR,
            "generated_at": self.generated_at,
            "source": self.source,
            "source_kind": self.source_kind,
            "save_timestamp": self.timestamp,
            "save_version": self.save_version,
            "world": {
                "name": self.name,
                "width": self.width,
                "height": self.height,
                "year": self.year,
                "meta": self.world_meta,
                "stats": self.world_stats,
                "laws": self.world_laws,
            },
            "counts": {
                "kingdoms": len(self.kingdoms),
                "live_kingdoms": len(self.live_kingdoms()),
                "cities": len(self.cities),
                "units": len(self.actors),
                "armies": len(self.armies),
                "wars": len(self.wars),
                "alliances": len(self.alliances),
            },
            "kingdoms": {str(k): v for k, v in sorted(self.kingdoms.items())},
            "cities": {str(k): v for k, v in sorted(self.cities.items())},
            "armies": {str(k): v for k, v in sorted(self.armies.items())},
            "wars": {str(k): v for k, v in sorted(self.wars.items())},
            "alliances": {str(k): v for k, v in sorted(self.alliances.items())},
            "relations": self.relations,
            "cultures": {str(k): v for k, v in sorted(self.cultures.items())},
            "languages": {str(k): v for k, v in sorted(self.languages.items())},
            "religions": {str(k): v for k, v in sorted(self.religions.items())},
            "subspecies": {str(k): v for k, v in sorted(self.subspecies.items())},
            "clans": {str(k): v for k, v in sorted(self.clans.items())},
            "plots": {str(k): v for k, v in sorted(self.plots.items())},
            "world_log": self.world_log,
        }


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def _index_by_id(rows: Any) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in _as_list(rows):
        if isinstance(row, dict) and isinstance(row.get("id"), int):
            out[row["id"]] = row
    return out


def load_world(folder: SaveFolder, *, with_stats: bool = True) -> WorldSnapshot:
    """Decode one save folder and build every derived index."""
    payload = decode_payload(folder.payload)
    snap = WorldSnapshot(
        save_version=_as_int(payload.get("saveVersion"), 0),
        width=_as_int(payload.get("width"), 0),
        height=_as_int(payload.get("height"), 0),
        timestamp=folder.timestamp,
        source=str(folder.payload),
        source_kind=folder.kind,
    )
    import time

    snap.generated_at = time.time()

    snap.world_meta = dict(payload.get("mapStats") or {})
    snap.world_stats = dict(payload.get("mapStats") or {})
    laws = payload.get("worldLaws")
    if isinstance(laws, dict):
        snap.world_laws = [str(x) for x in _as_list(laws.get("list"))]
    elif isinstance(laws, list):
        snap.world_laws = [str(x) for x in laws]

    snap.year = _as_int(snap.world_stats.get("history_current_year"), 0)

    snap.kingdoms = _index_by_id(payload.get("kingdoms"))
    snap.cities = _index_by_id(payload.get("cities"))
    snap.armies = _index_by_id(payload.get("armies"))
    snap.wars = _index_by_id(payload.get("wars"))
    snap.alliances = _index_by_id(payload.get("alliances"))
    snap.cultures = _index_by_id(payload.get("cultures"))
    snap.languages = _index_by_id(payload.get("languages"))
    snap.religions = _index_by_id(payload.get("religions"))
    snap.subspecies = _index_by_id(payload.get("subspecies"))
    snap.clans = _index_by_id(payload.get("clans"))
    snap.families = _index_by_id(payload.get("families"))
    snap.plots = _index_by_id(payload.get("plots"))
    snap.books = _index_by_id(payload.get("books"))
    snap.actors = _index_by_id(payload.get("actors_data"))

    for row in _as_list(payload.get("relations")):
        if isinstance(row, dict):
            rel_id = _as_str(row.get("rel_id"))
            if rel_id:
                snap.relations[rel_id] = row

    _build_derived(snap, payload)

    if with_stats:
        snap.stats = StatsDatabase.open(folder.stats_db)
        if snap.stats.available:
            snap.world_log = snap.stats.log(limit=400)
            for kid in snap.kingdoms:
                snap.kingdom_log[kid] = snap.stats.log(limit=60, kingdom_id=kid)
    return snap


def _build_derived(snap: WorldSnapshot, payload: dict[str, Any]) -> None:
    for kid in snap.kingdoms:
        snap.kingdom_cities[kid] = []
        snap.kingdom_units[kid] = []
        snap.kingdom_armies[kid] = []
        snap.kingdom_territory[kid] = 0
        snap.kingdom_buildings[kid] = 0
        snap.kingdom_wars[kid] = []

    for city in snap.cities.values():
        kid = city.get("kingdomID")
        if isinstance(kid, int):
            snap.kingdom_cities.setdefault(kid, []).append(city["id"])
            # A city owns one tile per entry in `zones`, so len(zones) is its
            # land area -- the closest thing the save has to "territory".
            snap.kingdom_territory[kid] = snap.kingdom_territory.get(kid, 0) + len(
                _as_list(city.get("zones"))
            )
        snap.city_units.setdefault(city["id"], [])

        # Centroid of the city's tiles: one (x, y) a leader can hand to the
        # player as the spot to drop food, ore, or a power. City records carry
        # no coordinates of their own, so the zones are the only source.
        zones = [z for z in _as_list(city.get("zones"))
                 if isinstance(z, dict) and isinstance(z.get("x"), int)
                 and isinstance(z.get("y"), int)]
        if zones:
            snap.city_centers[city["id"]] = [
                int(round(sum(z["x"] for z in zones) / len(zones))),
                int(round(sum(z["y"] for z in zones) / len(zones))),
            ]

    for actor in snap.actors.values():
        kid = actor.get("civ_kingdom_id")
        if isinstance(kid, int) and kid in snap.kingdom_units:
            snap.kingdom_all_units[kid] = snap.kingdom_all_units.get(kid, 0) + 1
            if _is_boat_asset(_as_str(actor.get("asset_id"))):
                snap.kingdom_boats[kid] = snap.kingdom_boats.get(kid, 0) + 1
            else:
                snap.kingdom_units[kid].append(actor["id"])
        cid = actor.get("cityID")
        if isinstance(cid, int):
            snap.city_all_units[cid] = snap.city_all_units.get(cid, 0) + 1
            if _is_boat_asset(_as_str(actor.get("asset_id"))):
                snap.city_boats[cid] = snap.city_boats.get(cid, 0) + 1
            else:
                snap.city_units.setdefault(cid, []).append(actor["id"])

    for army in snap.armies.values():
        kid = army.get("id_kingdom")
        if isinstance(kid, int) and kid in snap.kingdom_armies:
            snap.kingdom_armies[kid].append(army["id"])

    # Army strength comes from the units themselves: a soldier carries the
    # `army` field holding its army id. City/kingdom records do NOT store army
    # size or loyalty in saveVersion 17, so counting units is the only correct
    # source (verified against KingdomYearly1.army, which matched within the
    # drift expected from combat losses since the yearly tick).
    for actor in snap.actors.values():
        army_id = actor.get("army")
        if isinstance(army_id, int):
            snap.army_size_map[army_id] = snap.army_size_map.get(army_id, 0) + 1

    # Buildings -> city -> kingdom, and housing counts per city.
    occupancy: dict[int, int] = {}
    for actor in snap.actors.values():
        hb = actor.get("homeBuildingID")
        if isinstance(hb, int):
            occupancy[hb] = occupancy.get(hb, 0) + 1
    houses_by_city: dict[int, int] = {}
    for building in _as_list(payload.get("buildings")):
        if not isinstance(building, dict):
            continue
        bid = building.get("id")
        cid = building.get("cityID")
        if not isinstance(cid, int) or cid < 0:
            continue
        if isinstance(bid, int):
            snap.city_building_index.setdefault(cid, []).append(bid)
        city = snap.cities.get(cid)
        if city is None:
            continue
        kid = city.get("kingdomID")
        if isinstance(kid, int) and kid in snap.kingdom_buildings:
            snap.kingdom_buildings[kid] += 1
        # A building is a "house" when at least one unit calls it home; the
        # game's own yearly stats use the same notion (housed vs homeless).
        if isinstance(bid, int) and occupancy.get(bid):
            houses_by_city[cid] = houses_by_city.get(cid, 0) + 1

    for cid in snap.cities:
        housed = sum(1 for uid in snap.city_units.get(cid, ())
                     if isinstance(snap.actors.get(uid, {}).get("homeBuildingID"), int))
        snap.city_housing[cid] = {
            "houses": houses_by_city.get(cid, 0),
            "housed": housed,
            "homeless": max(0, len(snap.city_units.get(cid, ())) - housed),
        }

    for war in snap.wars.values():
        for side in ("list_attackers", "list_defenders"):
            for kid in _as_list(war.get(side)):
                if isinstance(kid, int) and kid in snap.kingdom_wars:
                    if war["id"] not in snap.kingdom_wars[kid]:
                        snap.kingdom_wars[kid].append(war["id"])

    # Real coordinate bounds. The save's own `width`/`height` are engine chunk
    # counts (4x4 in a normal world) and NOT map dimensions -- reporting them as
    # map size would badly mislead anyone choosing a spot to act on. Measuring
    # the actual x/y values is the only trustworthy source.
    xs: list[int] = []
    ys: list[int] = []
    for center in snap.city_centers.values():
        xs.append(center[0])
        ys.append(center[1])
    for actor in snap.actors.values():
        if isinstance(actor.get("x"), int):
            xs.append(actor["x"])
        if isinstance(actor.get("y"), int):
            ys.append(actor["y"])
    for building in _as_list(payload.get("buildings")):
        if isinstance(building, dict):
            if isinstance(building.get("mainX"), int):
                xs.append(building["mainX"])
            if isinstance(building.get("mainY"), int):
                ys.append(building["mainY"])
    if xs and ys:
        snap.coord_bounds = [min(xs), min(ys), max(xs), max(ys)]
