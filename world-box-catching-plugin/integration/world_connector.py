#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""world_connector.py —— WorldBox 世界服务适配器（给"房间程序"用）

一个房间程序（多 agent 对话平台）只需要 `WorldConnector` 这一个类，
就能把 WorldBox 的真实世界状态接进对话，并在世界推进时驱动新一轮决策。

设计要点（为什么这样写）：

* **只有标准库依赖。** 直接 `import` 或复制进你的工程即可，不用 pip install。
* **两种后端自动切换。** 优先走 `worldbox_agent.py serve` 提供的 HTTP 接口；
  接口没开时自动回退直接读磁盘上的 JSON 快照。房间程序因此不会因为
  "用户忘了启动服务"而整个挂掉。
* **世界推进 = 快照指纹变化。** 游戏的自动存档是天然节拍器：指纹不变，
  世界就没变，平台**不应该**发起新一轮决策（否则 agent 会在同一帧上
  反复决策、反复提出同一条政策）。
* **平台级上下文是压缩过的。** 完整国情约 27 KB，直接塞进每个 agent 的
  context 既贵又没必要。`agent_context()` 把它压到 2 KB 左右，只保留
  决策真正要用的数字与趋势；需要细节时用 `full_self()` 按需取。
* **只读。** 本适配器绝不修改游戏。agent 的政策只是"提案"，
  由创世者（玩家）执行；执行结果通过 `ActionJournal` 回填。

典型用法见文件末尾的 `__main__` 演示，或 docs/世界房间程序对接文档.md。

如果你接的是 Agent Room（桌面上的 `space` 目录），那边有一份 Node 版的
等价实现 `worldbox-bridge.js`，接入步骤见它的 docs/世界房间接入指南.md。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "WorldConnector", "WorldStatus", "ActionJournal", "ActionItem",
    "STALE_AFTER_SECONDS", "ConnectorError",
]

# 存档超过这个秒数就认为"世界数据已过期"，游戏多半暂停或关闭了。
STALE_AFTER_SECONDS = 600


class ConnectorError(RuntimeError):
    """连不上世界数据（服务没开、快照不存在、格式不对）。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class WorldStatus:
    """一份快照的"身份"。指纹不变 = 世界没推进。"""

    fingerprint: str
    source: str
    save_timestamp: float
    save_age_seconds: float
    read_at: float
    world_name: str
    year: int
    kingdoms: int
    live_kingdoms: int
    stats_db: bool
    backend: str                 # "http" | "file"
    stale: bool
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "source": self.source,
            "save_timestamp": self.save_timestamp,
            "save_age_seconds": self.save_age_seconds,
            "read_at": self.read_at,
            "world_name": self.world_name,
            "year": self.year,
            "kingdoms": self.kingdoms,
            "live_kingdoms": self.live_kingdoms,
            "stats_db": self.stats_db,
            "backend": self.backend,
            "stale": self.stale,
            "warning": self.warning,
        }


@dataclass
class ActionItem:
    """一条由 agent 提出、等待创世者执行的政策动作。"""

    id: str
    kingdom_id: int
    kingdom_name: str
    kind: str                    # "落点型" | "开关型"
    summary: str                 # 人类可读摘要，直接显示在房间里
    target: Optional[str] = None      # 城市名
    coord: Optional[list[int]] = None  # [x, y]
    detail: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"      # pending | proposed | executed | rejected | obsolete
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    decision_note: str = ""
    world_year: int = 0
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kingdom_id": self.kingdom_id,
            "kingdom_name": self.kingdom_name, "kind": self.kind,
            "summary": self.summary, "target": self.target, "coord": self.coord,
            "detail": self.detail, "status": self.status,
            "created_at": self.created_at, "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "world_year": self.world_year, "fingerprint": self.fingerprint,
        }


class ActionJournal:
    """政策动作的账本：提案 → 创世者裁决 → 回填结果。

    平台需要它是因为 **agent 改不了游戏**。agent 只能提案，
    创世者（玩家）照着做。所以必须有一条闭环：

        提案(action) → 玩家执行/拒绝 → 回填状态 → 下一轮把结果告诉 agent

    下一轮把"上一轮你提的 X 已执行/被拒绝"注入 context，
    agent 才知道自己说话有没有用、才知道世界为什么变了。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._items: dict[str, ActionItem] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._path = path
        self._seq = 0
        if path and path.is_file():
            self._load()

    # -- 写入 ---------------------------------------------------------
    def propose(self, kingdom_id: int, kingdom_name: str, kind: str,
                summary: str, *, target: Optional[str] = None,
                coord: Optional[list[int]] = None,
                detail: Optional[dict] = None,
                world_year: int = 0, fingerprint: str = "") -> ActionItem:
        with self._lock:
            self._seq += 1
            item = ActionItem(
                id=f"a{int(time.time())}-{self._seq}",
                kingdom_id=kingdom_id, kingdom_name=kingdom_name,
                kind=kind, summary=summary, target=target, coord=coord,
                detail=detail or {}, world_year=world_year,
                fingerprint=fingerprint, status="proposed",
            )
            self._items[item.id] = item
            self._order.append(item.id)
            self._persist()
            return item

    def decide(self, action_id: str, status: str, note: str = "") -> Optional[ActionItem]:
        """创世者裁决。status 取 executed / rejected。"""
        if status not in ("executed", "rejected", "obsolete"):
            raise ValueError("status 必须是 executed / rejected / obsolete")
        with self._lock:
            item = self._items.get(action_id)
            if item is None:
                return None
            item.status = status
            item.decided_at = time.time()
            item.decision_note = note
            self._persist()
            return item

    def expire_stale_pending(self, current_fingerprint: str) -> list[ActionItem]:
        """世界推进后，把上一帧还没裁决的提案标记为 obsolete。

        不这样做的话，agent 会在新的一帧里继续看到上一帧的旧提案，
        误以为"我提过了但没人理"——而实际上那条提案针对的局势已经过去了。
        """
        expired = []
        with self._lock:
            for item in self._items.values():
                if item.status == "proposed" and item.fingerprint != current_fingerprint:
                    item.status = "obsolete"
                    item.decided_at = time.time()
                    item.decision_note = "世界已推进，该提案针对的局势已过去"
                    expired.append(item)
            if expired:
                self._persist()
        return expired

    # -- 读取 ---------------------------------------------------------
    def pending(self) -> list[ActionItem]:
        with self._lock:
            return [self._items[i] for i in self._order
                    if self._items[i].status == "proposed"]

    def for_kingdom(self, kingdom_id: int) -> list[ActionItem]:
        with self._lock:
            return [self._items[i] for i in self._order
                    if self._items[i].kingdom_id == kingdom_id]

    def recent_decided(self, kingdom_id: Optional[int] = None,
                       limit: int = 6) -> list[ActionItem]:
        """最近已裁决的动作——用来告诉 agent"你上次说的那件事结果如何"。"""
        with self._lock:
            items = [self._items[i] for i in self._order
                     if self._items[i].status in ("executed", "rejected", "obsolete")
                     and (kingdom_id is None or self._items[i].kingdom_id == kingdom_id)]
        items.sort(key=lambda x: x.decided_at or 0, reverse=True)
        return items[:limit]

    def all_items(self) -> list[ActionItem]:
        with self._lock:
            return [self._items[i] for i in self._order]

    def export_execution_list(self) -> list[dict[str, Any]]:
        """给创世者看的"待执行清单"——房间 UI 直接渲染这个。"""
        out = []
        for item in self.pending():
            row = {
                "动作": item.summary, "国家": item.kingdom_name,
                "类型": item.kind, "编号": item.id,
            }
            if item.coord:
                row["落点坐标"] = f"{item.target}({item.coord[0]},{item.coord[1]})"
            elif item.target:
                row["目标"] = item.target
            out.append(row)
        return out

    # -- 持久化 -------------------------------------------------------
    def _persist(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([self._items[i].to_dict() for i in self._order],
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass  # 账本写不进去不该让平台崩

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for row in raw if isinstance(raw, list) else []:
            try:
                item = ActionItem(**{k: v for k, v in row.items()
                                     if k in ActionItem.__dataclass_fields__})
            except TypeError:
                continue
            self._items[item.id] = item
            self._order.append(item.id)
        self._seq = len(self._order)


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------

class WorldConnector:
    """把 WorldBox 世界状态接进房间程序。

    参数
    ----
    base_url : worldbox_agent 服务地址，默认 http://127.0.0.1:8777
    data_dir : 输出目录（后端回退时读它的 JSON），默认 ./data
    timeout  : 单次请求超时秒数
    cache_ttl: 同一快照的响应缓存秒数，避免房间里 N 个 agent 打同一份数据
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8777",
                 data_dir: str | os.PathLike = "data",
                 *, timeout: float = 20.0, cache_ttl: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._backend: Optional[str] = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._index: Optional[dict] = None

    # -- 后端探测 ------------------------------------------------------
    @property
    def backend(self) -> str:
        if self._backend is None:
            self._backend = "http" if self._http("/health") is not None else "file"
        return self._backend

    def _http(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        url = self.base_url + path
        if params:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        key = "H:" + url
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_ttl:
                return hit[1]
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, json.JSONDecodeError, ValueError):
            return None
        with self._lock:
            self._cache[key] = (now, data)
        return data

    def _file(self, *parts: str) -> Optional[Any]:
        path = self.data_dir.joinpath(*parts)
        key = "F:" + str(path)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_ttl:
                return hit[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        with self._lock:
            self._cache[key] = (now, data)
        return data

    # -- 世界状态 ------------------------------------------------------
    def status(self) -> WorldStatus:
        """当前快照的身份与新鲜度。房间的"节拍器"就靠它。"""
        if self.backend == "http":
            health = self._http("/health") or {}
            snap = health.get("snapshot") or {}
            if not snap:
                raise ConnectorError(
                    "worldbox_agent 服务在运行，但还没有读到任何存档。"
                    "请确认 WorldBox 已载入世界并自动存档过一次。")
            age = float(snap.get("save_age_seconds") or 0.0)
            fp = f"{snap.get('source')}|{snap.get('save_timestamp')}"
            return WorldStatus(
                fingerprint=fp, source=str(snap.get("source")),
                save_timestamp=float(snap.get("save_timestamp") or 0),
                save_age_seconds=age, read_at=float(snap.get("read_at") or time.time()),
                world_name=str(snap.get("world_name") or "?"),
                year=int(snap.get("year") or 0),
                kingdoms=int(snap.get("kingdoms") or 0),
                live_kingdoms=int(snap.get("live_kingdoms") or 0),
                stats_db=bool(snap.get("stats_db")),
                backend="http", stale=age > STALE_AFTER_SECONDS,
                warning=self._stale_warning(age),
            )

        index = self._file("_index.json")
        if not index:
            raise ConnectorError(
                f"既连不上 {self.base_url}，也读不到 {self.data_dir} 里的快照。"
                "请先运行 `python worldbox_agent.py serve`（推荐），"
                "或 `python worldbox_agent.py export` 生成一次快照。")
        age = float(index.get("存档距今秒数") or 0.0)
        ts = float(index.get("生成时间") or time.time())
        return WorldStatus(
            fingerprint=f"file|{index.get('世界名称')}|{index.get('年份')}|{ts}",
            source=str(self.data_dir), save_timestamp=ts, save_age_seconds=age,
            read_at=ts, world_name=str(index.get("世界名称") or "?"),
            year=int(index.get("年份") or 0),
            kingdoms=int(index.get("王国数量") or 0),
            live_kingdoms=sum(1 for k in (index.get("kingdoms") or [])
                              if k.get("是否存续")),
            stats_db=True, backend="file", stale=age > STALE_AFTER_SECONDS,
            warning=self._stale_warning(age),
        )

    @staticmethod
    def _stale_warning(age: float) -> str:
        if age <= STALE_AFTER_SECONDS:
            return ""
        return (f"⚠️ 世界数据已 {age / 60:.1f} 分钟旧"
                f"（阈值 {STALE_AFTER_SECONDS // 60} 分钟），"
                "游戏可能已暂停或关闭。请勿当作实时局势。")

    # -- 国家清单 ------------------------------------------------------
    def nations(self) -> list[dict[str, Any]]:
        """所有国家（含已灭亡）。平台用它建立"国家 → agent"的映射。"""
        if self.backend == "http":
            data = self._http("/api/v1/nations", {"compact": "1"})
        else:
            data = self._file("_index.json")
        if not data:
            return []
        return list(data.get("kingdoms") or [])

    def world_overview(self) -> dict[str, Any]:
        if self.backend == "http":
            return self._http("/api/v1/world", {"compact": "1"}) or {}
        return self._file("world_overview.json") or {}

    def full_self(self, nation: str | int) -> dict[str, Any]:
        """完整国情（约 27 KB）。**不要每轮都注入**，只在 agent 明确要细节时用。"""
        token = urllib.parse.quote(str(nation))
        if self.backend == "http":
            return self._http(f"/api/v1/nations/{token}", {"compact": "1"}) or {}
        return self._file_from_nation(nation, "self.json") or {}

    def others(self, nation: str | int) -> dict[str, Any]:
        """列国简报（约 4 KB）：他国军力、城市坐标、领土主张线索。"""
        token = urllib.parse.quote(str(nation))
        if self.backend == "http":
            return self._http(f"/api/v1/nations/{token}/others", {"compact": "1"}) or {}
        return self._file_from_nation(nation, "others.json") or {}

    def history(self, nation: str | int, limit: int = 12,
                table: str = "KingdomYearly1") -> list[dict[str, Any]]:
        token = urllib.parse.quote(str(nation))
        if self.backend == "http":
            data = self._http(f"/api/v1/nations/{token}/history",
                              {"history": table, "limit": str(limit), "compact": "1"})
            return list((data or {}).get("series") or [])
        data = self._file_from_nation(nation, "history.json") or {}
        return list((data.get("series") or [])[-limit:])

    def events(self, nation: str | int, limit: int = 12) -> list[dict[str, Any]]:
        token = urllib.parse.quote(str(nation))
        if self.backend == "http":
            data = self._http(f"/api/v1/nations/{token}/events",
                              {"limit": str(limit), "compact": "1"})
            return list((data or {}).get("events") or [])
        data = self._file_from_nation(nation, "events.json") or {}
        return list((data.get("events") or [])[-limit:])

    def _file_from_nation(self, nation: str | int, filename: str) -> Optional[Any]:
        """按国名或 id 找到对应的国家目录。"""
        index = self._file("_index.json") or {}
        slug = None
        mapping = index.get("id到目录") or {}
        if isinstance(nation, int) or str(nation).isdigit():
            slug = (mapping.get(str(nation)) or "").split("/")[-1]
        if not slug:
            by_name = index.get("按名称查id") or {}
            kid = by_name.get(str(nation))
            if isinstance(kid, list):
                kid = kid[0] if kid else None
            if kid is not None:
                slug = (mapping.get(str(kid)) or "").split("/")[-1]
        if not slug:
            slug = str(nation)
        return self._file("nations", slug, filename)

    # -- 平台级上下文构建 ------------------------------------------------
    def agent_context(self, nation: str | int, *,
                      history_limit: int = 10,
                      others_limit: int = 8,
                      city_limit: int = 8,
                      recent_actions: Optional[list[ActionItem]] = None) -> dict[str, Any]:
        """为**一个 agent** 准备本轮上下文（约 2–4 KB）。

        这是平台最该用的方法：它把"完整国情 + 列国简报 + 历年趋势"
        压成决策真正需要的形状，并附带：
          * 数据新鲜度（agent 必须知道自己在看多旧的世界）
          * 自己上一轮提案的结果（否则 agent 感觉自己在对着空气说话）

        返回的 dict 可以直接 `json.dumps` 后作为 system/user 消息的一部分
        注入给该国的 agent。

        关于 `city_limit`：大国的城市可能有几十座，全列出来会吃掉几千字节，
        而**决策只需要最要紧的那几座**。这里按"无家可归 → 人口"排序取前 N
        座（无家可归是真实的分城数据，且是叛乱前兆，优先看它）。
        需要完整城市清单时，用 `full_self()` 按需取。
        """
        ident = self.resolve(nation)
        if ident is None:
            return {"错误": f"找不到国家 {nation!r}",
                    "可用国家": [n.get("名称") for n in self.nations()]}

        kid = ident["id"]
        name = ident["名称"]
        self_view = self.full_self(kid)
        others_view = self.others(kid)
        hist = self.history(kid, limit=history_limit)
        status = self.status()

        last = hist[-1] if hist else {}
        first = hist[0] if hist else {}

        def series(field_name: str) -> Optional[dict[str, Any]]:
            values = [h.get(field_name) for h in hist if h.get(field_name) is not None]
            if not values:
                return None
            return {"年份区间": [first.get("timestamp"), last.get("timestamp")],
                    "数值": values, "变化": values[-1] - values[0]}

        def brief_city(c: dict[str, Any]) -> dict[str, Any]:
            row = {"名称": c.get("名称"), "坐标": c.get("坐标"),
                   "人口": c.get("人口"), "士兵": c.get("其中士兵")}
            if c.get("无家可归"):
                row["无家可归"] = c["无家可归"]
            if c.get("建筑数"):
                row["建筑数"] = c["建筑数"]
            return row

        rivals = []
        for n in (others_view.get("列国") or [])[:others_limit]:
            entry = {
                "名称": n.get("名称"), "立场": n.get("我方立场"),
                "军队": n.get("军队兵力"), "人口": n.get("人口"),
                "城市数": n.get("城市数"), "声望": n.get("声望"),
                "国王": n.get("国王"),
                "国王特质": [t.get("label") for t in (n.get("国王特质") or [])],
                "国家特质": [t.get("label") for t in (n.get("国家特质") or [])],
                "城市列表": [brief_city(c) for c in (n.get("城市列表") or [])],
            }
            if n.get("领土主张线索"):
                entry["领土主张线索"] = n["领土主张线索"]
            rivals.append(entry)

        my_cities = list(((self_view.get("territory") or {}).get("城市列表") or []))
        my_cities.sort(key=lambda c: (c.get("无家可归") or 0, c.get("人口") or 0),
                       reverse=True)

        ctx: dict[str, Any] = {
            "角色": f"你是 WorldBox 世界「{status.world_name}」中「{name}」的领袖。",
            "数据新鲜度": {
                "世界年份": status.year,
                "存档距今秒数": round(status.save_age_seconds, 1),
                "后端": status.backend,
                "世界指纹": status.fingerprint,
                "警告": status.warning or None,
            },
            "我的国家": {
                "编号": kid, "名称": name,
                "是否存续": ident.get("是否存续"),
                "国王": (self_view.get("ruler") or {}).get("国王"),
                "国王特质": [t.get("label")
                             for t in ((self_view.get("ruler") or {}).get("国王特质") or [])],
                "国家特质": [t.get("label")
                             for t in ((self_view.get("status") or {}).get("国家特质") or [])],
                "人口": (self_view.get("demographics") or {}).get("人口"),
                "军队": (self_view.get("military") or {}).get("军队总兵力"),
                "城市数": (self_view.get("territory") or {}).get("城市数"),
                "领土地块": (self_view.get("territory") or {}).get("领土地块"),
                "声望": (self_view.get("status") or {}).get("声望"),
                "声望排名": (self_view.get("status") or {}).get("声望排名"),
                "国库": last.get("money"), "粮食": last.get("food"),
                "饥饿": (self_view.get("demographics") or {}).get("饥饿"),
                "迁出": (self_view.get("status") or {}).get("迁出人数"),
                "迁入": (self_view.get("status") or {}).get("迁入人数"),
                "城市总数": (self_view.get("territory") or {}).get("城市数"),
                "城市列表_按无家可归排序_仅前若干座": [
                    brief_city(c) for c in my_cities[:city_limit]
                ],
                "_城市列表说明": (
                    f"这里只列了最要紧的 {min(city_limit, len(my_cities))} 座"
                    f"（共 {len(my_cities)} 座），按无家可归人数排序。"
                    "需要全部城市时请读 self.json"
                ),
            },
            "趋势": {k: v for k, v in {
                "人口": series("population"), "军队": series("army"),
                "国库": series("money"), "粮食": series("food"),
                "饥饿": series("hungry"), "无家可归": series("homeless"),
                "领土地块": series("territory"),
            }.items() if v},
            "我的战争": [
                {"战争": w.get("战争名称"), "我方角色": w.get("我方角色"),
                 "敌方": [o.get("名称") for o in (w.get("敌方") or [])],
                 "我方阵亡": w.get("我方阵亡"), "敌方阵亡": w.get("敌方阵亡")}
                for w in ((self_view.get("diplomacy") or {}).get("战争") or [])
            ],
            "列国": rivals,
            "关系矩阵": others_view.get("关系矩阵") or {},
            "近期大事": [
                {"年份": e.get("年份"), "事件": e.get("事件")}
                for e in ((self_view.get("recent_events") or [])[-6:])
            ],
            "我上一轮提出的政策及结果": [
                {"摘要": a.summary, "状态": a.status,
                 "创世者回复": a.decision_note or None}
                for a in (recent_actions or [])
            ],
            "坐标范围": ((self.world_overview().get("world") or {})
                        .get("可用坐标范围")),
        }
        return ctx

    # -- 名字解析 ------------------------------------------------------
    def resolve(self, token: str | int) -> Optional[dict[str, Any]]:
        """把 国名 / id / #id / 名字子串 解析成一个国家条目。"""
        nations = self.nations()
        if not nations:
            return None
        if isinstance(token, int) or str(token).lstrip("#").isdigit():
            kid = int(str(token).lstrip("#"))
            return next((n for n in nations if n.get("id") == kid), None)
        want = str(token).strip().lower()
        exact = [n for n in nations if str(n.get("名称", "")).lower() == want]
        if len(exact) == 1:
            return exact[0]
        partial = [n for n in nations if want in str(n.get("名称", "")).lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            # 多义时取更强大者，并在返回里标明有歧义
            partial.sort(key=lambda n: (bool(n.get("是否存续")),
                                        n.get("声望") or 0), reverse=True)
            hit = dict(partial[0])
            hit["_歧义"] = [n.get("名称") for n in partial]
            return hit
        return None

    # -- 节拍器 --------------------------------------------------------
    def has_advanced(self, last: Optional[WorldStatus]) -> bool:
        """世界是否推进了。房间的主循环用它决定要不要开新一轮。"""
        try:
            now = self.status()
        except ConnectorError:
            return False
        if last is None:
            return True
        return now.fingerprint != last.fingerprint

    def wait_for_advance(self, last: Optional[WorldStatus], *,
                         timeout: float = 900.0, poll: float = 10.0,
                         on_poll=None) -> Optional[WorldStatus]:
        """阻塞等到世界推进（或超时）。返回新的 WorldStatus。

        游戏自动存档约每 5 分钟一次，所以 timeout 建议 ≥ 900 秒。
        `on_poll(status)` 可用于给房间 UI 打"仍在等待"的心跳。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                now = self.status()
            except ConnectorError:
                now = None
            if now is not None and self.has_advanced(last):
                return now
            if on_poll and now is not None:
                on_poll(now)
            time.sleep(poll)
        return None

    # -- agent 政策产出的解析 ------------------------------------------
    @staticmethod
    def parse_policy_actions(policy_markdown: str,
                             kingdom_id: int,
                             kingdom_name: str) -> list[dict[str, Any]]:
        """从《国策》Markdown 里抽取可执行动作，转成结构化条目。

        这是**宽松**解析：世界里的 LLM 输出格式总会有出入，抽不到就返回空，
        平台应当把原始 Markdown 一并留给创世者看，不能因为解析失败就丢内容。

        期望的格式（见 worldbox-leader-policy skill）：
            - 执行：请创世者在 北平(4,23) 增建/升级住房，容量 +83
            - 执行（全国开关）：把「地方高税」下调一档
        """
        import re

        actions: list[dict[str, Any]] = []
        # 落点型： 请创世者在 城市名(12,34) 做某事
        site = re.compile(
            r"执行[^\n:：]*[:：]\s*(?P<who>请创世者)?[^\n]*?"
            r"(?P<city>[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{1,20}?)\s*[（(]\s*"
            r"(?P<x>\d{1,4})\s*[,，]\s*(?P<y>\d{1,4})\s*[)）]"
            r"(?P<rest>[^\n]*)")
        # 开关型： 执行（全国开关）：把 xxx 调到 yyy
        switch = re.compile(r"执行\s*[（(]\s*全国开关\s*[)）]\s*[:：]\s*(?P<rest>[^\n]+)")

        for line in policy_markdown.splitlines():
            stripped = line.strip()
            if not stripped.startswith("-") and "执行" not in stripped:
                continue
            m = switch.search(stripped)
            if m:
                actions.append({
                    "kind": "开关型", "target": None, "coord": None,
                    "summary": m.group("rest").strip(" 。"),
                    "raw": stripped,
                })
                continue
            m = site.search(stripped)
            if m:
                actions.append({
                    "kind": "落点型",
                    "target": (m.group("city") or "").strip(),
                    "coord": [int(m.group("x")), int(m.group("y"))],
                    "summary": ((m.group("city") or "").strip()
                                + m.group("rest").strip(" 。"))[:160],
                    "raw": stripped,
                })
        return actions


# ---------------------------------------------------------------------------
# 演示：一个最小可跑的房间主循环骨架
# ---------------------------------------------------------------------------

def _demo() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="WorldConnector 自检 / 演示")
    ap.add_argument("--base-url", default="http://127.0.0.1:8777")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--nation", default=None, help="演示某国的 agent 上下文")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    args = ap.parse_args()

    conn = WorldConnector(args.base_url, args.data_dir)
    try:
        st = conn.status()
    except ConnectorError as exc:
        print(f"[错误] {exc}")
        return 2

    print(f"后端        : {st.backend}")
    print(f"世界        : {st.world_name}  第 {st.year} 年")
    print(f"国家        : 存活 {st.live_kingdoms} / 共 {st.kingdoms}")
    print(f"存档距今    : {st.save_age_seconds:.0f} 秒")
    print(f"世界指纹    : {st.fingerprint}")
    if st.warning:
        print(f"{st.warning}")

    print("\n国家清单：")
    for n in conn.nations():
        print(f"  #{n['id']:<3} {n['名称']:<16} 人口={n['人口']:<6} "
              f"军队={n['军队兵力']:<5} 声望={n['声望']}")

    target = args.nation
    if not target:
        alive = [n for n in conn.nations() if n.get("是否存续")]
        target = alive[0]["名称"] if alive else None
    if target:
        ctx = conn.agent_context(target, history_limit=6)
        payload = json.dumps(ctx, ensure_ascii=False, indent=1)
        print(f"\n给「{target}」的 agent 上下文（{len(payload.encode('utf-8'))} 字节）：")
        print(payload[:1800])
        if len(payload) > 1800:
            print("  ...（已截断显示）")

    # 演示政策解析
    sample = """
**A. 落点型措施**
1. **补住房** —— 执行：请创世者在 北平(4,23) 增建/升级住房，容量 +83
2. **压饥饿** —— 执行：请创世者在 荆州(21,8) 投放食物约 25 人份
**B. 开关型措施**
3. **减税** —— 执行（全国开关）：把「地方高税」下调一档，试行一年
"""
    parsed = WorldConnector.parse_policy_actions(sample, 6, str(target))
    print(f"\n政策解析演示：抽到 {len(parsed)} 条动作")
    for a in parsed:
        print(f"  [{a['kind']}] {a.get('target') or '-'} {a.get('coord') or '-'} "
              f"{a['summary'][:50]}")

    if args.once:
        return 0

    print("\n演示 ActionJournal（提案 → 创世者裁决 → 回填）：")
    journal = ActionJournal(Path(args.data_dir) / "actions.json")
    item = journal.propose(6, str(target), "落点型",
                           "在 北平(4,23) 增建住房，容量 +83",
                           target="北平", coord=[4, 23],
                           world_year=st.year, fingerprint=st.fingerprint)
    print(f"  提案 id={item.id}  状态={item.status}")
    journal.decide(item.id, "executed", "已按提案在北平增建 90 人份住房")
    print(f"  裁决后状态={journal.for_kingdom(6)[-1].status}")
    print(f"  待执行清单: {json.dumps(journal.export_execution_list(), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
