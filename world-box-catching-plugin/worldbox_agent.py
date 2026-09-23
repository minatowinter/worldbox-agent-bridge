"""WorldBox 国家情报外置工具（简体中文版）。

    python worldbox_agent.py doctor                 # 自检：能不能读到游戏数据
    python worldbox_agent.py export                 # 导出一次快照到 ./data
    python worldbox_agent.py watch                  # 持续保持 ./data 最新
    python worldbox_agent.py serve                  # 后台盯盘 + 本地 HTTP 接口
    python worldbox_agent.py list                   # 列出存档里的所有国家
    python worldbox_agent.py show "国名"            # 打印某个国家的完整情报
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from worldbox_agent import __version__, SCHEMA_VERSION  # noqa: E402
from worldbox_agent import paths  # noqa: E402
from worldbox_agent.build import StatsDatabase, load_world  # noqa: E402
from worldbox_agent.export import export_all  # noqa: E402
from worldbox_agent.views import (kingdom_self_view, nation_index,  # noqa: E402
                                  others_view, slugify, world_overview)

DEFAULT_OUT = "data"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _resolve_latest(data_dir: str, *, include_saves: bool) -> paths.SaveFolder:
    root = paths.resolve_data_dir(data_dir)
    folder = paths.latest_save(root, include_saves=include_saves)
    if folder is None:
        raise SystemExit(
            f"在 {root} 下没有找到任何世界快照。\n"
            "  - 请启动 WorldBox 并载入一个世界（自动存档间隔约 5 分钟），\n"
            "  - 或者在游戏里按一次保存，然后重试。"
        )
    return folder


def _load(args, *, with_stats: bool = True, out: Optional[Path] = None):
    """按当前参数（含 --save）加载一份世界快照。"""
    folder = _pick_save(args, out)
    if folder is None:
        raise SystemExit(2)
    snap = load_world(folder, with_stats=with_stats)
    return folder, snap


def _out(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _echo(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()


def _find_kingdom(snap, needle: str) -> Optional[int]:
    """Match by id (``#7``), exact name, case-insensitive, or substring."""
    needle = needle.strip()
    if not needle:
        return None
    if needle.startswith("#") and needle[1:].isdigit():
        kid = int(needle[1:])
        return kid if kid in snap.kingdoms else None
    if needle.isdigit() and int(needle) in snap.kingdoms:
        return int(needle)

    lowered = needle.lower()
    for kingdom in snap.kingdoms.values():
        if str(kingdom.get("name", "")).lower() == lowered:
            return kingdom["id"]
    for kingdom in snap.kingdoms.values():
        if lowered in str(kingdom.get("name", "")).lower():
            return kingdom["id"]
    return None


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    _echo(f"worldbox-agent {__version__}  （数据结构版本 {SCHEMA_VERSION}）")
    _echo(f"python 版本      : {sys.version.split()[0]}  ({sys.executable})")
    _echo(f"当前工作目录     : {Path.cwd()}")
    _echo("")
    _echo("正在查找 WorldBox 用户数据目录 ...")
    found = paths.find_data_dir()
    if found is None:
        _echo("  没有找到。已尝试以下位置：")
        for cand in paths.candidate_data_dirs():
            _echo(f"    - {cand}")
        _echo("  可以设置环境变量 WORLDBOX_DATA_DIR，或用 --data-dir 指定。")
        return 2
    _echo(f"  找到: {found}")

    folders = list(paths.iter_save_folders(found))
    _echo(f"\n存档目录数量: {len(folders)}")
    for folder in folders[:8]:
        age = folder.age_seconds()
        _echo(f"  [{folder.kind:8}] {folder.timestamp:.0f}  "
              f"距今 {age/60:7.1f} 分钟  "
              f"载荷={folder.payload.name:9} "
              f"统计库={'有' if folder.stats_db else '无'}")
    if not folders:
        _echo("  还没有存档 —— 请在游戏里载入世界，等它自动存档一次。")
        return 2

    latest = paths.latest_save(found)
    _echo(f"\n本次选用: {latest.path}  ({latest.kind})")
    try:
        snap = load_world(latest)
    except Exception as exc:  # noqa: BLE001 - doctor 只报告，不崩溃
        _echo(f"  解析失败: {type(exc).__name__}: {exc}")
        return 3

    _echo(f"  存档格式版本 : {snap.save_version}")
    _echo(f"  世界         : {snap.name}  ({snap.width}x{snap.height})  第 {snap.year} 年")
    _echo(f"  王国         : {len(snap.kingdoms)} 个（存活 {len(snap.live_kingdoms())} 个）")
    _echo(f"  城市         : {len(snap.cities)}")
    _echo(f"  角色         : {len(snap.actors)}")
    _echo(f"  战争         : {len(snap.wars)}   同盟: {len(snap.alliances)}")
    db = snap.stats
    _echo(f"  统计数据库   : {'可用' if db and db.available else '不可用'}"
          f"{'' if db and db.available else '  (' + (db.error if db else 'n/a') + ')'}")
    if db and db.available:
        top = snap.live_kingdoms()
        rows = len(db.history("KingdomYearly1", top[0]["id"])) if top else 0
        _echo(f"    数据表     : {len(db.tables)} 张")
        _echo(f"    世界事件   : 最近 {len(db.log(limit=500))} 条")
        _echo(f"    逐年统计   : 最强国家有 {rows} 行历史")
    _echo("\n一切正常。下一步:  python worldbox_agent.py serve")
    return 0


def _pick_save(args, out: Optional[Path] = None) -> Optional[paths.SaveFolder]:
    """按 --save 选择（或跟随上次选择／最新）一份存档。"""
    root = paths.resolve_data_dir(args.data_dir)
    folder = paths.resolve_save(
        root, getattr(args, "save", None),
        include_saves=not args.autosaves_only,
        state_fallback=out,
    )
    if folder is None:
        _echo(f"在 {root} 下没有找到任何世界快照。\n"
              "  - 请启动 WorldBox 并载入一个世界（自动存档间隔约 5 分钟），\n"
              "  - 或者在游戏里按一次保存，然后重试。")
    return folder


def cmd_saves(args) -> int:
    root = paths.resolve_data_dir(args.data_dir)
    rows = paths.list_saves(root, include_saves=not args.autosaves_only,
                            limit=args.limit)
    selected = paths.load_selection(root, _out(args.out) if args.out else None)
    if args.json:
        _echo(json.dumps({"数据目录": str(root), "当前选择": selected,
                          "存档数量": len(rows), "saves": rows},
                         ensure_ascii=False, indent=1))
        return 0
    if not rows:
        _echo(f"{root} 下没有存档。")
        return 0
    _echo(f"数据目录: {root}")
    _echo(f"当前选择: {selected or '（未锁定，跟随最新）'}")
    _echo("")
    _echo(f"{'#':>3} {'文件夹':<12} {'类型':<5} {'时间':<20} {'距今':<10} "
          f"{'世界':<20} {'统计':<4} {'大小':>7}")
    for row in rows:
        mark = " *" if selected and row["key"] == selected else ""
        _echo(f"{row['index']:>3} {row['key']:<12} {row['kind_label']:<5} "
              f"{row['time_local']:<20} {row['age_text']:<10} "
              f"{(row['world_name'] or '(未知)'):<20} "
              f"{('有' if row['has_stats'] else '无'):<4} "
              f"{row['size_mb']:>6.2f}MB{mark}"
              + ("  ⚠️过期" if row["stale"] else ""))
    _echo("")
    _echo("锁定某份存档:  python worldbox_agent.py select #1")
    _echo("恢复跟随最新:  python worldbox_agent.py select latest")
    return 0


def cmd_select(args) -> int:
    root = paths.resolve_data_dir(args.data_dir)
    out = _out(args.out)
    raw = str(args.selector).strip()

    # "跟随最新"是指令，不是存档名——必须先拦截，
    # 否则 resolve_save 会把它当成"读取上次选择"，反而锁死在上一次的存档上。
    if raw.lower() in ("latest", "auto", "newest", "none", "-", "clear"):
        wrote = paths.save_selection(root, None, fallback=out)
        latest = paths.latest_save(root, include_saves=not args.autosaves_only)
        if latest is None:
            _echo("已清除存档选择，但当前没有任何存档。")
            return 0
        _echo(f"已恢复「跟随最新存档」。当前最新: {latest.key}  "
              f"({latest.world_name() or '未知世界'}, {latest.describe()['age_text']})")
        if not wrote:
            _echo("  警告: 状态没能写入磁盘（游戏数据目录可能不可写）。")
        return 0

    folder = paths.resolve_save(root, raw, fallback_to_latest=False,
                                include_saves=not args.autosaves_only,
                                state_fallback=out)
    if folder is None:
        _echo(f"没有匹配到 {raw!r} 的存档。可先运行: "
              "python worldbox_agent.py saves")
        return 2
    wrote = paths.save_selection(root, folder.key, fallback=out)
    _echo(f"已选中存档: {folder.key}  ({folder.world_name() or '未知世界'}, "
          f"{folder.describe()['age_text']})")
    if not wrote:
        _echo("  警告: 选择没能写入磁盘，下次启动不会记住。"
              "（游戏数据目录可能不可写）")
    else:
        _echo(f"  选择已记住: {paths.state_path(root, out)}")
    return 0


def cmd_export(args) -> int:
    out = _out(args.out)
    folder = _pick_save(args, out)
    if folder is None:
        return 2
    snap = load_world(folder)
    manifest = export_all(snap, out, history_table=args.history_table,
                          include_history_files=not args.no_history_files)
    if args.json:
        _echo(json.dumps(manifest, ensure_ascii=False, indent=1))
    else:
        src_age = manifest["source"]["save_age_seconds"]
        _echo(f"已导出 {manifest['counts']['kingdoms']} 个国家"
              f"（存活 {manifest['counts']['live_kingdoms']} 个） -> {out}")
        _echo(f"  文件数={manifest['counts']['files']}  "
              f"总字节={manifest['counts']['total_bytes']}  "
              f"耗时 {manifest['export_duration_ms']} 毫秒")
        _echo(f"  导出时存档已距今 {src_age:.0f} 秒   来源 {folder.path}")
        _echo(f"  建议从这里开始读: {out / '_index.json'}")
    return 0


def cmd_watch(args) -> int:
    out = _out(args.out)
    interval = max(2.0, float(args.interval))
    _echo(f"每 {interval:.0f} 秒检查一次新存档 -> {out}")
    last_stamp: Optional[float] = None
    last_source: Optional[str] = None
    while True:
        try:
            folder, snap = _load(args, out=out)
            stamp = folder.timestamp
            if stamp != last_stamp or str(folder.payload) != last_source or args.force:
                manifest = export_all(snap, out, history_table=args.history_table,
                                      include_history_files=not args.no_history_files)
                last_stamp, last_source = stamp, str(folder.payload)
                _echo(f"[{time.strftime('%H:%M:%S')}] {snap.name} 第 {snap.year} 年: "
                      f"存活国家 {manifest['counts']['live_kingdoms']} 个, "
                      f"{manifest['counts']['files']} 个文件, "
                      f"{manifest['export_duration_ms']} 毫秒 "
                      f"(存档本身已 {manifest['source']['save_age_seconds']:.0f} 秒旧)")
            elif args.verbose:
                _echo(f"[{time.strftime('%H:%M:%S')}] 没有新存档 "
                      f"(当前存档已 {folder.age_seconds():.0f} 秒旧)")
        except KeyboardInterrupt:
            _echo("\n已停止。")
            return 0
        except Exception as exc:  # noqa: BLE001 - 盯盘程序不能因为一次错误退出
            _echo(f"[{time.strftime('%H:%M:%S')}] 出错: {type(exc).__name__}: {exc}")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            _echo("\n已停止。")
            return 0


def cmd_list(args) -> int:
    _, snap = _load(args)
    index = nation_index(snap, {kid: slugify(str(kingdom.get("name") or ""),
                                             fallback=f"王国{kid}")
                                for kid, kingdom in snap.kingdoms.items()})
    if args.json:
        _echo(json.dumps(index, ensure_ascii=False, indent=1))
        return 0
    _echo(f"世界: {snap.name}   第 {snap.year} 年   "
          f"存档已 {max(0.0, time.time() - snap.timestamp):.0f} 秒")
    _echo(f"{'编号':>4}  {'存活':<4} {'人口':>6} {'城市':>4} {'军队':>5} {'声望':>7}  国名")
    for entry in index["kingdoms"]:
        kid = entry["id"]
        _echo(f"{kid:>4}  {('是' if entry['是否存续'] else '否'):<4} "
              f"{entry['人口']:>6} {entry['城市数']:>4} "
              f"{entry['军队兵力']:>5} {entry['声望']:>7}  {entry['名称']}")
    return 0


def cmd_show(args) -> int:
    _, snap = _load(args)
    kid = _find_kingdom(snap, args.nation)
    if kid is None:
        _echo(f"没有找到匹配 {args.nation!r} 的国家。可先运行: "
              "python worldbox_agent.py list")
        return 2
    if args.others:
        payload = others_view(snap, kid)
    elif args.world:
        payload = world_overview(snap)
    else:
        payload = kingdom_self_view(snap, kid, history_table=args.history_table)
    _echo(json.dumps(payload, ensure_ascii=False,
                     indent=None if args.compact else 1))
    return 0


def cmd_serve(args) -> int:
    from worldbox_agent.server import serve

    return serve(host=args.host, port=args.port, data_dir=args.data_dir,
                 out_dir=_out(args.out), interval=args.interval,
                 include_saves=not args.autosaves_only,
                 history_table=args.history_table, once=args.once,
                 open_browser=args.open_browser, quiet=args.quiet,
                 save_selector=args.save)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldbox_agent",
        description="WorldBox 0.51.2 国家情报外置工具（非 mod），"
                    "为 AI agent 提供分国家的实时世界状态。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version",
                        version=f"worldbox-agent {__version__} (schema {SCHEMA_VERSION})")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, out: bool = True):
        p.add_argument("--data-dir", default=None,
                       help="WorldBox 用户数据目录（默认自动查找）")
        p.add_argument("--save", default=None,
                       help="用哪份存档：文件夹名(时间戳)、#序号、世界名，"
                            "或 latest 跟随最新（默认读上次选择，没有则用最新）")
        p.add_argument("--history-table", default="KingdomYearly1",
                       choices=["KingdomYearly1", "KingdomYearly10", "KingdomYearly100"],
                       help="历史数据的时间分辨率（默认 KingdomYearly1 = 每年一行）")
        p.add_argument("--autosaves-only", action="store_true",
                       help="忽略手动保存的世界，只读自动存档")
        if out:
            p.add_argument("--out", default=DEFAULT_OUT,
                           help=f"输出目录（默认 {DEFAULT_OUT}）")

    p = sub.add_parser("saves", help="列出所有可选存档（有多份存档时先看这个）")
    common(p)
    p.add_argument("--limit", type=int, default=0, help="最多列几份（0=全部）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_saves)

    p = sub.add_parser("select", help="锁定要用的存档，并记住这个选择")
    common(p)
    p.add_argument("selector",
                   help="文件夹名(时间戳)、#序号、世界名，或 latest 恢复跟随最新")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("doctor", help="自检：能否找到并解析游戏数据")
    common(p, out=False)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("export", help="导出一次快照到输出目录")
    common(p)
    p.add_argument("--json", action="store_true", help="以 JSON 输出清单")
    p.add_argument("--no-history-files", action="store_true",
                   help="不生成每个国家的 history.json / events.json")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("watch", help="游戏一写新存档就重新导出")
    common(p)
    p.add_argument("--interval", type=float, default=10.0,
                   help="轮询间隔秒数（默认 10）")
    p.add_argument("--verbose", action="store_true", help="没有新存档时也输出日志")
    p.add_argument("--force", action="store_true", help="每次轮询都强制重新导出")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("list", help="列出最新存档里的所有国家")
    common(p, out=False)
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="以 JSON 打印某个国家的完整情报")
    common(p, out=False)
    p.add_argument("nation", help="国名、国名的一部分，或 #编号")
    p.add_argument("--others", action="store_true", help="改看『列国简报』")
    p.add_argument("--world", action="store_true", help="改看『世界概览』")
    p.add_argument("--compact", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("serve", help="盯盘并对外提供 HTTP 接口给 agent 调用")
    common(p)
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    p.add_argument("--port", type=int, default=8777, help="监听端口（默认 8777）")
    p.add_argument("--interval", type=float, default=10.0,
                   help="存档轮询间隔秒数（默认 10）")
    p.add_argument("--once", action="store_true",
                   help="只导出一次，之后不再盯盘")
    p.add_argument("--open-browser", action="store_true",
                   help="启动后用浏览器打开接口首页")
    p.add_argument("--quiet", action="store_true", help="减少控制台输出")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _echo("\n已中断。")
        return 130
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        _echo(f"错误: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
