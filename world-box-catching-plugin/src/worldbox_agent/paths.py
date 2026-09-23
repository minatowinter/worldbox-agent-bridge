"""Discovery of WorldBox user-data directories, saves and autosaves.

The tool never guesses a single hard-coded path: it walks the known Unity
``LocalLow`` roots plus a few user-supplied overrides, so it works whether the
game is installed through Steam, GOG or a portable folder.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Unity company/product names used by WorldBox.
COMPANY_NAMES = ("mkarpenko", "WorldBox", "worldbox")
PRODUCT_NAMES = ("WorldBox", "worldbox")

# 存档超过这个秒数就认为数据过期（与 views/server 保持一致）。
STALE_AFTER_SECONDS = 600


@dataclass(frozen=True)
class SaveFolder:
    """A folder that holds one world snapshot (``map.wbax`` / ``map.wbox``)."""

    path: Path
    payload: Path            # map.wbax (JSON) or map.wbox (zlib JSON)
    stats_db: Optional[Path]  # map_stats.s3db, may be missing
    meta: Optional[Path]      # map.meta
    timestamp: float
    kind: str                 # "autosave" | "save"

    @property
    def is_compressed(self) -> bool:
        return self.payload.suffix.lower() == ".wbox"

    @property
    def key(self) -> str:
        """稳定且可手输的标识：就是文件夹名（一个 unix 时间戳）。"""
        return self.path.name

    def age_seconds(self, now: Optional[float] = None) -> float:
        return (now if now is not None else _now()) - self.timestamp

    def world_name(self) -> str:
        """不解析 24 MB 存档就拿到世界名。

        ``map.meta`` 里有 ``mapStats.name`` 且只有几 KB，
        所以列出存档列表时不必把大文件整个解出来。
        """
        if self.meta is not None:
            try:
                data = json.loads(self.meta.read_text(encoding="utf-8-sig"))
                stats = data.get("mapStats") or {}
                name = stats.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                pass
        return ""

    def size_bytes(self) -> int:
        try:
            return self.payload.stat().st_size
        except OSError:
            return 0

    def describe(self, now: Optional[float] = None) -> dict:
        """给"选存档"界面用的一切信息，且不碰大文件。"""
        import time as _t

        age = self.age_seconds(now)
        return {
            "key": self.key,
            "path": str(self.path),
            "kind": self.kind,
            "kind_label": "手动存档" if self.kind == "save" else "自动存档",
            "timestamp": self.timestamp,
            "time_local": _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(self.timestamp)),
            "age_seconds": round(age, 1),
            "age_text": humanise_age(age),
            "world_name": self.world_name(),
            "has_stats": self.stats_db is not None,
            "compressed": self.is_compressed,
            "size_mb": round(self.size_bytes() / 1024 / 1024, 2),
            "stale": age > STALE_AFTER_SECONDS,
        }


def humanise_age(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    if seconds < 90:
        return f"{seconds:.0f} 秒前"
    if seconds < 5400:
        return f"{seconds / 60:.1f} 分钟前"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{seconds / 86400:.1f} 天前"


def _now() -> float:
    import time

    return time.time()


def candidate_data_dirs() -> list[Path]:
    """Every plausible ``...\\mkarpenko\\WorldBox`` directory, best first."""
    roots: list[Path] = []

    home = Path.home()
    localappdata = os.environ.get("LOCALAPPDATA")
    userprofile = os.environ.get("USERPROFILE")

    # Standard Unity path: %USERPROFILE%\AppData\LocalLow\<company>\<product>
    locallow_roots: list[Path] = []
    if userprofile:
        locallow_roots.append(Path(userprofile) / "AppData" / "LocalLow")
    if localappdata:
        locallow_roots.append(Path(localappdata) / "LocalLow")

    for ll in locallow_roots:
        for company in COMPANY_NAMES:
            for product in PRODUCT_NAMES:
                roots.append(ll / company / product)

    # Non-standard overrides an operator may set.
    for env in ("WORLDBOX_DATA_DIR", "WORLDBOX_DIR", "WORLDBOX_SAVE_DIR"):
        raw = os.environ.get(env)
        if raw:
            roots.insert(0, Path(raw))

    # A couple of common alternate locations.
    if userprofile:
        roots.append(Path(userprofile) / "Documents" / "WorldBox")
        roots.append(Path(home) / "AppData" / "Roaming" / "worldbox")

    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def find_data_dir() -> Optional[Path]:
    """First existing WorldBox user-data directory, or ``None``."""
    for d in candidate_data_dirs():
        try:
            if d.is_dir():
                return d
        except OSError:
            continue
    return None


def _folder_from(path: Path, kind: str, stats_hint: Optional[Path] = None) -> Optional[SaveFolder]:
    payload: Optional[Path] = None
    for name in ("map.wbax", "map.wbox"):
        cand = path / name
        if cand.is_file():
            payload = cand
            break
    if payload is None:
        return None

    stats = stats_hint if (stats_hint and stats_hint.is_file()) else None
    if stats is None:
        cand = path / "map_stats.s3db"
        stats = cand if cand.is_file() else None

    meta = path / "map.meta"
    meta = meta if meta.is_file() else None

    # Prefer the folder name (a unix timestamp); fall back to mtime, then meta.
    ts: Optional[float] = None
    try:
        ts = float(path.name)
    except ValueError:
        try:
            ts = payload.stat().st_mtime
        except OSError:
            ts = None
    if ts is None:
        ts = _now()

    return SaveFolder(path=path, payload=payload, stats_db=stats, meta=meta,
                      timestamp=ts, kind=kind)


def iter_save_folders(data_dir: Path) -> Iterable[SaveFolder]:
    """All save folders under *data_dir*, newest first."""
    found: list[SaveFolder] = []

    for kind, sub in (("autosave", "autosaves"), ("save", "saves")):
        base = data_dir / sub
        if not base.is_dir():
            continue
        try:
            children = [c for c in base.iterdir() if c.is_dir()]
        except OSError:
            continue
        for child in children:
            folder = _folder_from(child, kind)
            if folder is not None:
                found.append(folder)

    found.sort(key=lambda f: (f.timestamp, f.payload.stat().st_mtime
                              if _safe_exists(f.payload) else 0.0), reverse=True)
    return found


def _safe_exists(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def latest_save(data_dir: Path, *, include_autosaves: bool = True,
                include_saves: bool = True) -> Optional[SaveFolder]:
    """Newest world snapshot, preferring the one with a stats database.

    Autosaves and manual saves are interleaved by timestamp; a snapshot that
    also has ``map_stats.s3db`` wins over a slightly newer one without it,
    because the history tables are what give agents memory of the past.
    """
    folders = [f for f in iter_save_folders(data_dir)
               if (f.kind == "autosave" and include_autosaves)
               or (f.kind == "save" and include_saves)]
    if not folders:
        return None

    with_stats = [f for f in folders if f.stats_db is not None]
    if with_stats:
        newest_stats = with_stats[0]
        newest_any = folders[0]
        # Only prefer a stats-less snapshot if it is dramatically newer
        # (e.g. a fresh manual save in a brand-new world).
        if newest_any.timestamp > newest_stats.timestamp + 900:
            return newest_any
        return newest_stats
    return folders[0]


def resolve_data_dir(explicit: Optional[str] = None) -> Path:
    """Resolve the data dir, raising a helpful error when it cannot be found."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_dir():
            raise FileNotFoundError(f"--data-dir does not exist: {p}")
        return p

    found = find_data_dir()
    if found is None:
        searched = "\n  ".join(str(p) for p in candidate_data_dirs())
        raise FileNotFoundError(
            "Could not locate the WorldBox user-data folder. Searched:\n  "
            f"{searched}\n"
            "Pass --data-dir explicitly, or set WORLDBOX_DATA_DIR."
        )
    return found


# --------------------------------------------------------------------------
# 选择存档
# --------------------------------------------------------------------------
#
# 为什么需要这个：一个正常的 WorldBox 用户会有几十个存档，而且分属不同世界、
# 不同时间（实测同一台机器上两组存档相差 4 小时）。如果工具永远只取"最新"，
# 多 agent 房间就会被接到一个使用者根本不关心的世界上——或者接到一个几小时前
# 的旧世界却不自知。所以"用哪份存档"必须是一个显式可选项。

STATE_FILENAME = "worldbox_selection.json"


def state_path(data_dir: Path, fallback: Optional[Path] = None) -> Path:
    """选择状态文件的位置。

    首选写在 WorldBox 数据目录里（跟着数据走，换项目也不会丢），
    但那个目录**可能不可写**（权限、只读挂载、沙箱）。所以允许传一个
    *fallback* 目录；调用方通常传工具自己的输出目录。
    """
    primary = Path(data_dir) / STATE_FILENAME
    if fallback is not None and not _dir_writable(Path(data_dir)):
        return Path(fallback) / STATE_FILENAME
    return primary


def _dir_writable(directory: Path) -> bool:
    try:
        if not directory.is_dir():
            return False
        probe = directory / ".wb-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_selection(data_dir: Path, fallback: Optional[Path] = None) -> Optional[str]:
    """读取上次选中的存档 key（文件夹名），没有则返回 None。

    两个候选位置都可能有值：游戏数据目录可写时写在那边，沙箱/只读时只能写进
    工具自己的输出目录（见 :func:`save_selection`）。**以 updated_at 最新的那份
    为准**——只按"哪个文件先找到"会让一份旧选择盖住新选择，症状是"我明明选了
    它，服务却在读另一个世界"。

    注意 ``selected_save: null`` 是有效状态，含义是"跟随最新存档"；它同样参与
    比较，所以一份更新的 null 会正确地取消锁定。
    """
    best_at = float("-inf")
    best_key: Optional[str] = None
    found = False
    for path in _selection_candidates(data_dir, fallback):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            at = float(raw.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        if not found or at > best_at:
            found = True
            best_at = at
            key = raw.get("selected_save")
            best_key = key if isinstance(key, str) and key else None
    return best_key if found else None


def _selection_candidates(data_dir: Path, fallback: Optional[Path]) -> list[Path]:
    paths = [Path(data_dir) / STATE_FILENAME]
    if fallback is not None:
        alt = Path(fallback) / STATE_FILENAME
        if alt not in paths:
            paths.append(alt)
    return paths


def save_selection(data_dir: Path, key: Optional[str],
                   fallback: Optional[Path] = None) -> bool:
    """记住选择，返回是否真的写成功。

    选择只是便利功能，写失败不该中断主流程——但**调用方需要知道**
    有没有写成功，否则使用者会遇到"我选了它却不记住"这种最难查的问题。
    所以这里返回布尔值，并且依次尝试主位置与回退位置。
    """
    payload = json.dumps({"selected_save": key, "updated_at": _now()},
                         ensure_ascii=False, indent=1)
    wrote = False
    for path in _selection_candidates(data_dir, fallback):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            wrote = True
        except (OSError, TypeError, ValueError):
            continue
    return wrote


def find_save(data_dir: Path, key: str) -> Optional[SaveFolder]:
    """按文件夹名精确定位一份存档。"""
    if not key:
        return None
    for folder in iter_save_folders(data_dir):
        if folder.key == key:
            return folder
    return None


def resolve_save(data_dir: Path, selector: Optional[str] = None, *,
                 include_autosaves: bool = True,
                 include_saves: bool = True,
                 fallback_to_latest: bool = True,
                 state_fallback: Optional[Path] = None) -> Optional[SaveFolder]:
    """把使用者的选择解析成一份具体存档。

    *selector* 接受几种写法，按顺序尝试：

    * ``None`` / ``""`` / ``"latest"`` / ``"auto"`` → 上次选中的，或最新的一份
    * 序号 ``"#1"`` 或 ``"1"``（1 = 列表里最新的一份）
    * 文件夹名（即时间戳），如 ``"1789872887"``
    * 存档目录的完整或相对路径
    * 世界名的一部分，如 ``"Skulls"``

    解析不到时，若 *fallback_to_latest* 为真则退回最新一份；调用方可以比较
    返回值的 ``key`` 与请求值，判断选择是否真的生效。
    """
    folders = [f for f in iter_save_folders(data_dir)
               if (f.kind == "autosave" and include_autosaves)
               or (f.kind == "save" and include_saves)]
    if not folders:
        return None

    sel = (selector or "").strip()

    # 1) 没指定 → 上次选中的 → 最新
    if sel.lower() in ("", "latest", "auto", "newest"):
        remembered = load_selection(data_dir, state_fallback)
        if remembered:
            hit = next((f for f in folders if f.key == remembered), None)
            if hit is not None:
                return hit
        return latest_save(data_dir, include_autosaves=include_autosaves,
                           include_saves=include_saves)

    # 2) 序号（#1 或 1，只在 1–999 内当作序号，避免和 10 位时间戳冲突）
    if sel.startswith("#") and sel[1:].isdigit():
        idx = int(sel[1:]) - 1
        return folders[idx] if 0 <= idx < len(folders) else None
    if sel.isdigit() and len(sel) <= 3:
        idx = int(sel) - 1
        if 0 <= idx < len(folders):
            return folders[idx]

    # 3) 精确文件夹名
    hit = next((f for f in folders if f.key == sel), None)
    if hit is not None:
        return hit

    # 4) 路径
    try:
        candidate = Path(sel).expanduser()
        if candidate.is_dir():
            resolved = candidate.resolve()
            hit = next((f for f in folders if f.path.resolve() == resolved), None)
            if hit is not None:
                return hit
    except OSError:
        pass

    # 5) 世界名（取最新的同名存档，列表已按时间倒序）
    by_name = [f for f in folders if f.world_name() and sel.lower() in f.world_name().lower()]
    if by_name:
        return by_name[0]

    if fallback_to_latest:
        return latest_save(data_dir, include_autosaves=include_autosaves,
                           include_saves=include_saves)
    return None


def list_saves(data_dir: Path, *, include_autosaves: bool = True,
               include_saves: bool = True, limit: int = 0,
               selected_key: Optional[str] = None) -> list[dict]:
    """给选择界面用的存档清单（最新在前），不解析大文件。

    *selected_key* 若给出，则每行带 ``selected`` 标记，方便 UI 直接高亮。
    """
    folders = [f for f in iter_save_folders(data_dir)
               if (f.kind == "autosave" and include_autosaves)
               or (f.kind == "save" and include_saves)]
    now = _now()
    rows: list[dict] = []
    for index, folder in enumerate(folders, start=1):
        row = folder.describe(now)
        row["index"] = index
        row["selected"] = bool(selected_key) and folder.key == selected_key
        rows.append(row)
    if limit and limit > 0:
        rows = rows[:limit]
    return rows
