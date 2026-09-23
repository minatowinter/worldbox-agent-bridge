"""Write a snapshot to disk as per-nation, agent-readable documents.

Layout produced under the output directory::

    world.json                    whole world, raw-ish (canonical snapshot)
    world_overview.json           global situation + balance of power
    _index.json                   name -> id -> folder lookup (read this first)
    MANIFEST.json                 what was written, when, from which save
    nations/<slug>/self.json      one nation's full domestic dossier
    nations/<slug>/others.json    slim view of every rival from that nation's seat
    nations/<slug>/history.json   that nation's yearly time series
    nations/<slug>/events.json    that nation's event log
    nations/by-id/<id>.json       id -> self.json (no name lookup needed)
    nations/<slug>/README.txt     how an agent should use this folder
    agents/<nation>/persona.md    leader personality (written by the agent, NOT here)
    agents/<nation>/memory.md     leader memory    (written by the agent, NOT here)

``agents/`` is deliberately outside the prune scope: those are the agent's own
notes and must survive every re-export, otherwise the leader forgets who it is.

Everything is written atomically (temp file + ``os.replace``) so an agent reading
concurrently never sees a half-written document.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .build import WorldSnapshot
from .views import (EVENT_LABELS, kingdom_self_view, nation_index, others_view,
                    slugify, world_overview)

FILE_README = """国家情报档案 —— 请先读这里
================================

你是这个 WorldBox 世界里某一个国家的领袖。这个文件夹就是你的情报机构。

文件说明
--------
self.json     本国完整国情：国王、人口、城市、军队、外交、近期事件、逐年历史。
              **从这里开始读。**
others.json   其他所有国家的横向对比，以及战争/立场矩阵。做外交和军事决定前必读。
history.json  本国的逐年时间序列（人口、军队、国库、领土……）。用来看"趋势"。
events.json   按时间排列的、发生在本国身上的大事。

如何使用
--------
1. 先读 self.json，注意 `status.是否存续` —— 如果为 false，你的国家已经灭亡。
2. 任何外交/军事决策之前，先读 others.json。
3. 比较军力请看 `military.军队总兵力`，以及他国的 `军队兵力`，不要用人口去猜。
4. 看 `recent_events` 和 `history` 判断趋势，不要只看当前这一帧。

数据新鲜度
----------
每个文件里的 `world.存档距今秒数` 表示这份数据是多久以前从游戏里读出来的。
如果这个数字远大于刷新间隔，说明游戏可能暂停了或者已经关闭。
**绝对不要编造这些文件里没有的数字。**

关于数值口径
------------
* 人口 = 存档里归属本国的角色数量。
* 军队 = 这些角色中"正在军队编制内"的人数。
* 领土 = 本国所有城市所占地块数之和。
* 城市记录本身不保存军队、忠诚度、资源库存；这些请看 history。
* 存档不记录角色年龄，所以本工具不提供年龄，只提供世代与在位时间。
"""


def _atomic_write_json(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(data)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unique_slugs(snap: WorldSnapshot) -> dict[int, str]:
    """One folder name per kingdom, disambiguated with ``-<id>`` on collision."""
    seen: dict[str, int] = {}
    slugs: dict[int, str] = {}
    for kingdom in sorted(snap.kingdoms.values(), key=lambda k: k["id"]):
        kid = kingdom["id"]
        base = slugify(str(kingdom.get("name") or ""), fallback=f"kingdom-{kid}")
        slug = base
        if slug in seen:
            slug = f"{base}-{kid}"
        seen[slug] = kid
        slugs[kid] = slug
    return slugs


def export_all(snap: WorldSnapshot, out_dir: Path, *,
               history_table: str = "KingdomYearly1",
               prune: bool = True,
               include_history_files: bool = True) -> dict[str, Any]:
    """Materialise every document. Returns a manifest dict."""
    t0 = time.time()
    out_dir = Path(out_dir)
    nations_dir = out_dir / "nations"
    by_id_dir = nations_dir / "by-id"

    if prune and nations_dir.is_dir():
        shutil.rmtree(nations_dir, ignore_errors=True)
    nations_dir.mkdir(parents=True, exist_ok=True)
    by_id_dir.mkdir(parents=True, exist_ok=True)

    slugs = _unique_slugs(snap)
    written: list[dict[str, Any]] = []
    total_bytes = 0

    def emit(path: Path, payload: Any) -> None:
        nonlocal total_bytes
        size = _atomic_write_json(path, payload)
        total_bytes += size
        written.append({"path": str(path.relative_to(out_dir)).replace("\\", "/"),
                        "bytes": size})

    # -- world-level ---------------------------------------------------
    emit(out_dir / "world.json", snap.to_snapshot_dict())
    emit(out_dir / "world_overview.json", world_overview(snap))
    emit(out_dir / "_index.json", nation_index(snap, slugs))
    _atomic_write_text(out_dir / "README.txt", FILE_README)

    # -- per nation ----------------------------------------------------
    for kingdom in sorted(snap.kingdoms.values(), key=lambda k: k["id"]):
        kid = kingdom["id"]
        slug = slugs[kid]
        folder = nations_dir / slug
        self_view = kingdom_self_view(snap, kid, history_table=history_table)

        emit(folder / "self.json", self_view)
        emit(folder / "others.json", others_view(snap, kid))
        emit(by_id_dir / f"{kid}.json", self_view)

        if include_history_files:
            history = snap.history_of(kid, history_table, limit=200)
            emit(folder / "history.json", {
                "schema_version": "1.0",
                "kind": "nation.history",
                "文档类型": "本国逐年历史",
                "kingdom_id": kid,
                "王国名称": str(kingdom.get("name") or ""),
                "来源表": history_table,
                "说明": "每个游戏年份一行；游戏当年没统计到的字段会被省略",
                "series": history,
            })
            events = []
            for row in snap.kingdom_log.get(kid, []):
                asset = str(row.get("asset_id") or "")
                events.append({
                    "年份": row.get("timestamp"),
                    "事件": EVENT_LABELS.get(asset, asset),
                    "事件码": asset,
                    "主角": row.get("special1"),
                    "对象": row.get("special2"),
                    "补充": row.get("special3"),
                    "坐标": [row.get("x"), row.get("y")],
                })
            emit(folder / "events.json", {
                "schema_version": "1.0",
                "kind": "nation.events",
                "文档类型": "本国大事记",
                "kingdom_id": kid,
                "王国名称": str(kingdom.get("name") or ""),
                "events": events,
            })

        _atomic_write_text(folder / "README.txt", FILE_README)

    manifest = {
        "schema_version": "1.0",
        "kind": "manifest",
        "generated_at": snap.generated_at,
        "export_duration_ms": int((time.time() - t0) * 1000),
        "source": {"path": snap.source, "kind": snap.source_kind,
                   "save_timestamp": snap.timestamp,
                   "save_age_seconds": round(max(0.0, time.time() - snap.timestamp), 1)},
        "stats_db": {
            "available": bool(snap.stats and snap.stats.available),
            "error": snap.stats.error if snap.stats else "not opened",
        },
        "counts": {
            "kingdoms": len(snap.kingdoms),
            "live_kingdoms": len(snap.live_kingdoms()),
            "files": len(written),
            "total_bytes": total_bytes,
        },
        "out_dir": str(out_dir),
        "files": written,
        "agent_entrypoints": {
            "index": "_index.json",
            "world": "world_overview.json",
            "nation_self": "nations/<slug>/self.json",
            "nation_others": "nations/<slug>/others.json",
        },
    }
    _atomic_write_json(out_dir / "MANIFEST.json", manifest)
    return manifest
