"""Local HTTP API + background refresher for agent consumption.

Pure ``http.server`` -- no third-party packages. The server keeps exactly one
:class:`WorldSnapshot` in memory, rebuilds it whenever the game writes a new
save, and answers queries straight from that snapshot.

Endpoints (all ``GET``, all JSON, UTF-8):

    /                              small human-readable index
    /health                        liveness + snapshot freshness
    /api/v1/world                  global overview
    /api/v1/nations                index: name -> id -> folder
    /api/v1/nations/<id|name>      one nation's full domestic dossier
    /api/v1/nations/<id|name>/others      rivals, from that nation's seat
    /api/v1/nations/<id|name>/history     that nation's yearly time series
    /api/v1/nations/<id|name>/events      that nation's event log
    /api/v1/events                        world event log
    /api/v1/refresh                       force a re-read (POST or GET)

``<id|name>`` accepts ``7``, ``#7``, ``大仁`` or a unique substring. Query
params: ``?compact=1`` (no pretty indent), ``?history=10`` (years per row),
``?limit=N`` on list endpoints.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import SCHEMA_VERSION, __version__
from . import paths
from .build import WorldSnapshot, load_world
from .export import export_all
from .views import (kingdom_self_view, nation_index, others_view, slugify,
                    world_overview)


class SnapshotStore:
    """Thread-safe holder for the current snapshot, refreshed from disk."""

    def __init__(self, data_dir: Optional[str], out_dir: Path, *,
                 interval: float = 10.0, include_saves: bool = True,
                 history_table: str = "KingdomYearly1",
                 export_on_refresh: bool = True, logger=None,
                 save_selector: Optional[str] = None) -> None:
        self.data_dir = data_dir
        self.out_dir = out_dir
        self.interval = max(2.0, float(interval))
        self.include_saves = include_saves
        self.history_table = history_table
        self.export_on_refresh = export_on_refresh
        self._log = logger or (lambda *_: None)
        # 使用者选中的存档：文件夹名 / #序号 / 路径 / 世界名。
        # None 或 "auto" 表示"跟着最新存档走"。
        self._save_selector = (save_selector or "").strip() or None
        self._requested_selector = self._save_selector
        self._used_key: Optional[str] = None

        self._lock = threading.RLock()
        self._snapshot: Optional[WorldSnapshot] = None
        self._slugs: dict[int, str] = {}
        self._source_key: Optional[str] = None
        self._error: str = ""
        self._refreshes = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 存档选择 -------------------------------------------------------
    @property
    def _state_fallback(self) -> Path:
        """选择状态写不进游戏目录时，退到输出目录。"""
        return self.out_dir

    def saves(self, limit: int = 0) -> list[dict[str, Any]]:
        """可选的存档清单（最新在前）。"""
        try:
            root = paths.resolve_data_dir(self.data_dir)
        except FileNotFoundError:
            return []
        with self._lock:
            used = self._used_key
            requested = self._requested_selector
        rows = paths.list_saves(root, include_saves=self.include_saves,
                                limit=limit, selected_key=used or requested)
        return rows

    def select_save(self, selector: Optional[str], *, persist: bool = True) -> dict[str, Any]:
        """切换使用哪一份存档。立刻重新读取，成功与否都如实回报。"""
        sel = (selector or "").strip()
        # "latest"/"auto" 是「恢复跟随最新」这条指令，不是某份存档的名字。
        # 不拦截的话会被当成存档名，把选择锁死在当前这份上——
        # 使用者以为恢复自动跟随了，其实被钉住了。
        if sel.lower() in ("", "latest", "auto", "newest", "none", "-", "clear"):
            sel = None
        try:
            root = paths.resolve_data_dir(self.data_dir)
        except FileNotFoundError as exc:
            return {"ok": False, "error": str(exc)}

        with self._lock:
            self._requested_selector = sel
            self._save_selector = sel
        if persist:
            paths.save_selection(root, sel, fallback=self._state_fallback)

        state = self.refresh(force=True)
        if not state.get("ok"):
            return state
        with self._lock:
            used = self._used_key
        # 选择没生效（比如写错了名字）时要说清楚，而不是悄悄用别的存档
        warning = ""
        if sel and used != sel:
            warning = (f"没有匹配到 {sel!r}，已回退到 {used}。"
                       f"用 /api/v1/saves 查看可选存档。")
        return {**state, "selected": used, "requested": sel, "warning": warning}

    # -- state ---------------------------------------------------------
    @property
    def snapshot(self) -> Optional[WorldSnapshot]:
        with self._lock:
            return self._snapshot

    def health(self) -> dict[str, Any]:
        with self._lock:
            snap = self._snapshot
            used = self._used_key
            requested = self._requested_selector
            return {
                "ok": snap is not None,
                "schema_version": SCHEMA_VERSION,
                "tool_version": __version__,
                "refreshes": self._refreshes,
                "error": self._error,
                "data_dir": str(paths.resolve_data_dir(self.data_dir))
                            if self.data_dir or paths.find_data_dir() else None,
                "out_dir": str(self.out_dir),
                "interval_seconds": self.interval,
                "history_table": self.history_table,
                "save_selection": {
                    "requested": requested,
                    "using": used,
                    "mode": "跟随最新存档" if not requested else "已锁定指定存档",
                    "matches": (used == requested) if requested else None,
                },
                "snapshot": None if snap is None else {
                    "world_name": snap.name,
                    "year": snap.year,
                    "source": snap.source,
                    "source_kind": snap.source_kind,
                    "save_key": used,
                    "save_timestamp": snap.timestamp,
                    "save_age_seconds": round(max(0.0, time.time() - snap.timestamp), 1),
                    "read_at": snap.generated_at,
                    "kingdoms": len(snap.kingdoms),
                    "live_kingdoms": len(snap.live_kingdoms()),
                    "read_age_seconds": round(max(0.0, time.time() - snap.generated_at), 1),
                    "stats_db": bool(snap.stats and snap.stats.available),
                    # 统计库是副本还是原件：退回原件意味着"工具运行期间游戏可能
                    # 存不了档"，必须让人看得见（见 docs/外置读取原理.md 2.3）
                    "stats_copied": bool(snap.stats and snap.stats.copied),
                    "stats_note": (snap.stats.note if snap.stats else ""),
                },
            }

    # -- refresh -------------------------------------------------------
    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        try:
            root = paths.resolve_data_dir(self.data_dir)
        except FileNotFoundError as exc:
            with self._lock:
                self._error = str(exc)
            return {"ok": False, "error": str(exc)}

        with self._lock:
            selector = self._save_selector
        folder = paths.resolve_save(root, selector,
                                    include_saves=self.include_saves,
                                    state_fallback=self._state_fallback)
        if folder is None:
            msg = ("no world snapshot found -- load a world in WorldBox "
                   "(autosave is every ~5 minutes) or press Save in-game")
            with self._lock:
                self._error = msg
            return {"ok": False, "error": msg}

        key = f"{folder.payload}|{folder.timestamp}"
        with self._lock:
            self._used_key = folder.key
            unchanged = (key == self._source_key and self._snapshot is not None)
        if unchanged and not force:
            return {"ok": True, "unchanged": True, **self.health()}

        try:
            snap = load_world(folder, with_stats=True)
        except Exception as exc:  # noqa: BLE001 - keep serving the last good one
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            self._log(f"refresh failed: {self._error}")
            return {"ok": False, "error": self._error}

        slugs = {kid: slugify(str(kingdom.get("name") or ""), fallback=f"王国{kid}")
                 for kid, kingdom in snap.kingdoms.items()}
        with self._lock:
            old = self._snapshot
            self._snapshot = snap
            self._slugs = slugs
            self._source_key = key
            self._error = ""
            self._refreshes += 1

        if old is not None and old.stats is not None and old.stats is not snap.stats:
            old.stats.close()

        result: dict[str, Any] = {
            "ok": True, "unchanged": False,
            "source": str(folder.payload), "kind": folder.kind,
            "save_age_seconds": round(folder.age_seconds(), 1),
            "kingdoms": len(snap.kingdoms), "live_kingdoms": len(snap.live_kingdoms()),
            "year": snap.year,
        }
        if self.export_on_refresh:
            try:
                manifest = export_all(snap, self.out_dir,
                                      history_table=self.history_table)
                result["exported_files"] = manifest["counts"]["files"]
                result["export_ms"] = manifest["export_duration_ms"]
            except Exception as exc:  # noqa: BLE001 - export is best-effort
                result["export_error"] = f"{type(exc).__name__}: {exc}"
        return result

    # -- background thread ---------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.refresh(force=True)
        self._thread = threading.Thread(target=self._loop, name="wb-refresh",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.refresh()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- lookups -------------------------------------------------------
    def find_kingdom(self, token: str) -> Optional[int]:
        with self._lock:
            snap = self._snapshot
        if snap is None:
            return None
        token = unquote(token).strip()
        if not token:
            return None
        if token.startswith("#"):
            token = token[1:]
        if token.isdigit() and int(token) in snap.kingdoms:
            return int(token)

        lowered = token.lower()
        exact = [k["id"] for k in snap.kingdoms.values()
                 if str(k.get("name", "")).lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        partial = [k["id"] for k in snap.kingdoms.values()
                   if lowered in str(k.get("name", "")).lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            # prefer a live, more powerful match
            partial.sort(key=lambda kid: (bool(snap.kingdom_cities.get(kid)),
                                          snap.population_of(kid)), reverse=True)
            return partial[0]

        with self._lock:
            for kid, slug in self._slugs.items():
                if slug.lower() == lowered:
                    return kid
        return None

    def slugs(self) -> dict[int, str]:
        with self._lock:
            return dict(self._slugs)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = f"worldbox-agent/{__version__}"
    protocol_version = "HTTP/1.1"

    store: SnapshotStore  # injected by serve()

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "quiet", False):
            return
        self.server.log(f"{self.address_string()} {fmt % args}")  # type: ignore[attr-defined]

    def _send(self, payload: Any, status: int = 200, *, compact: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          indent=None if compact else 1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._route()

    def do_POST(self) -> None:  # noqa: N802
        self._route()

    def _route(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        compact = query.get("compact", ["0"])[0] not in ("0", "", "false")

        try:
            payload = self._dispatch(parts, query)
        except _HttpError as exc:
            self._send({"error": exc.message, "status": exc.status},
                       exc.status, compact=compact)
            return
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the server
            self._send({"error": f"{type(exc).__name__}: {exc}", "status": 500},
                       500, compact=compact)
            return

        if isinstance(payload, tuple):
            value, status = payload
            self._send(value, status, compact=compact)
        elif isinstance(payload, str):
            self._send_text(payload)
        else:
            self._send(payload, compact=compact)

    def _dispatch(self, parts: list[str], query: dict[str, list[str]]):
        store = self.store

        if not parts:
            return self._index_text()

        if parts[0] == "health" or parts == ["api", "v1", "health"]:
            return store.health()

        if parts[0] == "favicon.ico":
            return ("", 204)  # type: ignore[return-value]

        if parts[0] != "api":
            raise _HttpError(404, f"unknown path /{'/'.join(parts)}; try / or /api/v1/world")
        if len(parts) < 2 or parts[1] != "v1":
            raise _HttpError(404, "unknown api version; use /api/v1/...")

        rest = parts[2:]

        if not rest:
            return self._api_index()

        if rest[0] == "world":
            snap = self._require_snapshot()
            return world_overview(snap)

        if rest[0] == "refresh":
            if self.command not in ("GET", "POST"):
                raise _HttpError(405, "use GET or POST")
            return store.refresh(force=True)

        if rest[0] == "saves":
            # 列出可选存档；带 select 参数则切换。
            if len(rest) > 1 and rest[1] == "select":
                if self.command not in ("GET", "POST"):
                    raise _HttpError(405, "use GET or POST")
                selector = (query.get("key", [None])[0]
                            or query.get("save", [None])[0]
                            or (rest[2] if len(rest) > 2 else None))
                result = store.select_save(selector)
                return (result, 200 if result.get("ok") else 400)
            limit = _int_param(query, "limit", 0, 0, 5000)
            rows = store.saves(limit=limit)
            with_snapshot = store.health()
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": "world.saves",
                "数据目录": with_snapshot.get("data_dir"),
                "当前选择": with_snapshot.get("save_selection"),
                "存档数量": len(rows),
                "saves": rows,
                "用法": {
                    "锁定某份存档": "GET /api/v1/saves/select?key=<key 或 #序号>",
                    "恢复跟随最新": "GET /api/v1/saves/select?key=latest",
                },
            }

        if rest[0] == "events":
            snap = self._require_snapshot()
            limit = _int_param(query, "limit", 200, 1, 5000)
            kingdom = query.get("kingdom", [None])[0]
            kid = store.find_kingdom(kingdom) if kingdom else None
            return {"schema_version": SCHEMA_VERSION, "kind": "world.events",
                    "count": limit, "kingdom_id": kid,
                    "events": (snap.stats.log(limit=limit, kingdom_id=kid)
                               if snap.stats and snap.stats.available else [])}

        if rest[0] == "nations":
            snap = self._require_snapshot()
            slugs = store.slugs()
            if len(rest) == 1:
                return nation_index(snap, slugs)

            kid = store.find_kingdom(rest[1])
            if kid is None:
                raise _HttpError(404, f"no nation matches {rest[1]!r}")

            sub = rest[2] if len(rest) > 2 else "self"
            if sub == "self":
                view = kingdom_self_view(snap, kid, history_table=store.history_table)
                if view is None:
                    raise _HttpError(404, f"nation {kid} not found")
                return view
            if sub == "others":
                return others_view(snap, kid)
            if sub == "history":
                limit = _int_param(query, "limit", 200, 1, 5000)
                table = query.get("history", [store.history_table])[0]
                if table not in ("KingdomYearly1", "KingdomYearly10", "KingdomYearly100"):
                    raise _HttpError(400, "history must be KingdomYearly1/10/100")
                return {"schema_version": SCHEMA_VERSION, "kind": "nation.history",
                        "kingdom_id": kid, "table": table,
                        "series": snap.history_of(kid, table, limit=limit)}
            if sub == "events":
                limit = _int_param(query, "limit", 200, 1, 5000)
                return {"schema_version": SCHEMA_VERSION, "kind": "nation.events",
                        "kingdom_id": kid,
                        "events": (snap.stats.log(limit=limit, kingdom_id=kid)
                                   if snap.stats and snap.stats.available else [])}
            if sub == "brief":
                from .views import kingdom_brief

                brief = kingdom_brief(snap, kid)
                if brief is None:
                    raise _HttpError(404, f"nation {kid} not found")
                return brief
            raise _HttpError(404, f"unknown sub-resource {sub!r}; "
                                  "use self, others, history, events, brief")

        raise _HttpError(404, f"unknown resource {rest[0]!r}")

    # -- helpers -------------------------------------------------------
    def _require_snapshot(self) -> WorldSnapshot:
        snap = self.store.snapshot
        if snap is None:
            raise _HttpError(
                503,
                "no snapshot yet -- is WorldBox running with a world loaded? "
                "See /health for the exact error.",
            )
        return snap

    def _index_text(self) -> str:
        health = self.store.health()
        snap = health.get("snapshot") or {}
        lines = [
            f"worldbox-agent {__version__}  （数据结构版本 {SCHEMA_VERSION}）",
            "WorldBox 国家情报外置工具 —— 为 AI agent 提供分国家的世界状态。无需安装 mod。",
            "",
            f"世界         : {snap.get('world_name', '-')}   第 {snap.get('year', '-')} 年",
            f"存档距今     : {snap.get('save_age_seconds', '-')} 秒",
            f"国家         : 存活 {snap.get('live_kingdoms', '-')} / 共 {snap.get('kingdoms', '-')}",
            f"统计数据库   : {'可用' if snap.get('stats_db') else '不可用'}",
            f"最近错误     : {health.get('error') or '无'}",
            "",
            "可用接口",
            "  GET /health",
            "  GET /api/v1/world                        世界概览",
            "  GET /api/v1/nations                      列国索引（国名 -> 编号）",
            "  GET /api/v1/nations/<编号|国名>           某国完整国情",
            "  GET /api/v1/nations/<编号|国名>/others    某国视角下的列国简报",
            "  GET /api/v1/nations/<编号|国名>/history?history=KingdomYearly1",
            "  GET /api/v1/nations/<编号|国名>/events    某国大事记",
            "  GET /api/v1/events?limit=200             全球事件",
            "  GET /api/v1/refresh                      立即重新读取存档",
            "",
            "参数：compact=1 关闭美化缩进；limit=N 限制行数；history=时间分辨率",
            f"文件副本也同步写在: {health.get('out_dir')}",
        ]
        return "\n".join(lines) + "\n"

    def _api_index(self) -> dict[str, Any]:
        return {
            "名称": "worldbox-agent",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "接口列表": [
                "/health",
                "/api/v1/world",
                "/api/v1/nations",
                "/api/v1/nations/{编号或国名}",
                "/api/v1/nations/{编号或国名}/others",
                "/api/v1/nations/{编号或国名}/history",
                "/api/v1/nations/{编号或国名}/events",
                "/api/v1/nations/{编号或国名}/brief",
                "/api/v1/events",
                "/api/v1/refresh",
            ],
            "查询参数": {
                "compact": "=1 时不美化缩进",
                "limit": "history/events 的最大行数",
                "history": "KingdomYearly1 | KingdomYearly10 | KingdomYearly100",
            },
        }


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _int_param(query: dict[str, list[str]], name: str, default: int,
               lo: int, hi: int) -> int:
    raw = query.get(name, [None])[0]
    if raw is None:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, store: SnapshotStore,
                 quiet: bool = False) -> None:
        super().__init__(addr, handler)
        self.store = store
        self.quiet = quiet

    def log(self, message: str) -> None:
        print(f"[http] {message}", flush=True)

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        if not self.quiet:
            super().handle_error(request, client_address)


def serve(*, host: str = "127.0.0.1", port: int = 8777,
          data_dir: Optional[str] = None, out_dir: Path = Path("data"),
          interval: float = 10.0, include_saves: bool = True,
          history_table: str = "KingdomYearly1", once: bool = False,
          open_browser: bool = False, quiet: bool = False,
          save_selector: Optional[str] = None) -> int:
    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    store = SnapshotStore(data_dir, out_dir, interval=interval,
                          include_saves=include_saves,
                          history_table=history_table, logger=log,
                          save_selector=save_selector)

    log(f"worldbox-agent {__version__}（数据结构版本 {SCHEMA_VERSION}）")
    state = store.refresh(force=True)
    if not state.get("ok"):
        log(f"  警告: {state.get('error')}")
    else:
        log(f"  已读取世界快照，存活国家 {state.get('live_kingdoms')} 个"
            f"（第 {state.get('year')} 年）")
        sel = (state or {}) and store.health().get("save_selection") or {}
        if sel:
            using = sel.get("using") or "?"
            if sel.get("requested"):
                mark = "✔" if sel.get("matches") else "⚠"
                log(f"  {mark} 存档选择: 锁定 [{sel['requested']}] → 实际使用 {using}")
                if not sel.get("matches"):
                    log("     （选择没匹配上，已回退到最新。用 /api/v1/saves 查看可选存档）")
            else:
                log(f"  存档选择: 跟随最新存档（当前 {using}）")
        if state.get("exported_files"):
            log(f"  已导出 {state['exported_files']} 个文件到 {out_dir}，"
                f"耗时 {state.get('export_ms')} 毫秒")
        if state.get("export_error"):
            log(f"  导出出错: {state['export_error']}")
        if state.get("save_age_seconds", 0) > 900:
            log(f"  提示: 这份存档已经 {state['save_age_seconds'] / 60:.1f} 分钟旧。"
                "WorldBox 约每 5 分钟自动存档一次，也可以在游戏里手动保存以刷新。")

    if not once:
        store.start()
        log(f"  正在盯盘，每 {store.interval:.0f} 秒检查一次新存档")

    _Handler.store = store
    httpd: Optional[_Server] = None
    bound_port = port
    for attempt in range(10):
        try:
            httpd = _Server((host, bound_port), _Handler, store=store, quiet=quiet)
            break
        except OSError as exc:
            log(f"  端口 {bound_port} 不可用（{exc}），尝试 {bound_port + 1}")
            bound_port += 1
    if httpd is None:
        log("  没有可用端口，退出")
        return 4

    url = f"http://{host}:{bound_port}"
    log("")
    log(f"  接口已就绪: {url}")
    log(f"  例如:       {url}/api/v1/nations")
    log(f"  健康检查:   {url}/health")
    log("  按 Ctrl+C 停止")

    if open_browser:
        import webbrowser

        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("\n正在停止 ...")
    finally:
        httpd.shutdown()
        httpd.server_close()
        store.stop()
        snap = store.snapshot
        if snap is not None and snap.stats is not None:
            snap.stats.close()
    return 0
