#!/usr/bin/env python3
"""wb —— 国家情报查询小工具（给扮演国家领袖的 agent 用）。

它只是读数据，从不修改游戏。两种后端自动选择：
  1. 优先走本地 HTTP 接口（`python worldbox_agent.py serve` 提供的，数据最新）
  2. 接口不可用时，自动回退到磁盘上的 JSON 文件（data/nations/<国名>/...）

用法：
    python wb.py whoami                     # 我是谁？列出所有国家
    python wb.py whoami 大仁                # 用国名确认身份
    python wb.py self                       # 我的国家（需先 whoami 或 --me）
    python wb.py self --me 大仁             # 指定国家读完整国情
    python wb.py others --me 大仁           # 其他国家的横向对比
    python wb.py history --me 大仁 --last 5 # 只看最近 5 年的趋势
    python wb.py events --me 大仁           # 我的大事记
    python wb.py world                      # 世界概览
    python wb.py brief --me 大仁            # 极简摘要（最省 token）
    python wb.py raw nations                # 原始接口路径

约定：agent 的 system prompt 会告诉你扮演哪个国家，把它传给 --me，
或者设好环境变量 WB_NATION=<国名> 后就不必每次传。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_API = os.environ.get("WB_API", "http://127.0.0.1:8777")
DEFAULT_DATA = Path(os.environ.get("WB_DATA", "data"))

# 存档超过这个秒数就提醒数据可能过期（与工具主程序保持同一个阈值）。
STALE_AFTER_SECONDS = 600


# --------------------------------------------------------------------------
# 控制台编码：这个工具的输出全是中文，而 Windows 控制台默认是 GBK 代码页，
# 在 GBK 下中文常常被替换成 '?' 或乱码而不抛异常，agent 就会读到天书。
# 所以启动时主动把控制台切到 UTF-8，并把 Python 的 stdout/stderr 也切成 UTF-8。
# --------------------------------------------------------------------------

def _setup_io() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # 没有真实控制台（重定向到文件）时忽略
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_setup_io()


# --------------------------------------------------------------------------
# 后端：HTTP 优先，文件回退
# --------------------------------------------------------------------------

def _http_get(path: str) -> Optional[Any]:
    url = DEFAULT_API.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError):
        return None


def _read_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _slug_key(index: dict, nation: str) -> Optional[str]:
    """把用户给的国名映射到索引里的『目录』字段。"""
    kids = index.get("按名称查id") or {}
    lowered = nation.strip().lower()
    for name, kid in kids.items():
        if name.lower() == lowered:
            return str(kid)
    for name, kid in kids.items():
        if lowered in name.lower():
            return str(kid)
    return None


class Backend:
    """统一的取数入口，屏蔽 HTTP / 文件两种来源的差异。"""

    def __init__(self) -> None:
        self.mode = "http" if _http_get("/health") else "file"
        self._index: Optional[dict] = None

    # -- 基础文档 ------------------------------------------------------
    def index(self) -> dict:
        if self._index is None:
            data = _http_get("/api/v1/nations") if self.mode == "http" else None
            if data is None:
                data = _read_json(DEFAULT_DATA / "_index.json") or {}
                if data:
                    self.mode = "file"
            self._index = data
        return self._index or {}

    def world(self) -> dict:
        data = _http_get("/api/v1/world") if self.mode == "http" else None
        if data is None:
            data = _read_json(DEFAULT_DATA / "world_overview.json") or {}
        return data

    def _nation_file(self, nation: str, filename: str) -> Optional[dict]:
        index = self.index()
        slug = _slug_key(index, nation)
        folders: list[Path] = []
        if slug and isinstance(index.get("id到目录"), dict):
            rel = index["id到目录"].get(slug)
            if rel:
                folders.append(DEFAULT_DATA / rel)
        if not slug:
            folders.append(DEFAULT_DATA / "nations" / nation)
        for folder in folders:
            data = _read_json(folder / filename)
            if data is not None:
                return data
        return None

    def self_view(self, nation: str) -> Optional[dict]:
        if self.mode == "http":
            data = _http_get("/api/v1/nations/" + urllib.parse.quote(nation))
            if data is not None:
                return data
        return self._nation_file(nation, "self.json")

    def others(self, nation: str) -> Optional[dict]:
        if self.mode == "http":
            data = _http_get("/api/v1/nations/" + urllib.parse.quote(nation) + "/others")
            if data is not None:
                return data
        return self._nation_file(nation, "others.json")

    def history(self, nation: str, table: str, limit: int) -> Optional[dict]:
        if self.mode == "http":
            q = urllib.parse.urlencode({"history": table, "limit": limit})
            data = _http_get("/api/v1/nations/" + urllib.parse.quote(nation) + "/history?" + q)
            if data is not None:
                return data
        raw = self._nation_file(nation, "history.json")
        if raw is None:
            return None
        return {"series": (raw.get("series") or [])[-limit:], "table": raw.get("来源表")}

    def events(self, nation: str, limit: int) -> Optional[dict]:
        if self.mode == "http":
            data = _http_get("/api/v1/nations/" + urllib.parse.quote(nation)
                             + f"/events?limit={limit}")
            if data is not None:
                return data
        raw = self._nation_file(nation, "events.json")
        if raw is None:
            return None
        return {"events": (raw.get("events") or [])[-limit:]}

    def raw(self, path: str) -> Optional[Any]:
        if self.mode == "http":
            return _http_get(path)
        return None


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def _dump(payload: Any, compact: bool) -> None:
    text = json.dumps(payload, ensure_ascii=False,
                      indent=None if compact else 1)
    try:
        print(text)
    except UnicodeEncodeError:  # 极端情况下（控制台连 UTF-8 都不支持）
        sys.stdout.buffer.write(text.encode("utf-8", "replace"))
        sys.stdout.buffer.write(b"\n")


def _freshness_of(payload: Any) -> Optional[float]:
    """从任意一份文档里找出「存档距今秒数」。"""
    if not isinstance(payload, dict):
        return None
    for key in ("存档距今秒数",):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for key in ("world", "数据新鲜度"):
        nested = payload.get(key)
        found = _freshness_of(nested)
        if found is not None:
            return found
    return None


def _warn_if_stale(payload: Any) -> None:
    """数据过期时把警告写到 stderr，这样不会污染 agent 读的 JSON，
    但任何人类或 harness 都能立刻看到问题。"""
    age = _freshness_of(payload)
    if age is None or age <= STALE_AFTER_SECONDS:
        return
    print(
        f"\n⚠️  数据可能已过期：这份存档是 {age / 60:.1f} 分钟前的"
        f"（阈值 {STALE_AFTER_SECONDS // 60} 分钟）。\n"
        "    游戏可能已暂停或关闭。世界局势很可能已经变化，\n"
        "    请把这一点告诉使用者，不要把它当作实时战况。",
        file=sys.stderr,
    )


def _emit(payload: Any, compact: bool) -> None:
    """输出文档，并在数据过期时附带警告。"""
    _dump(payload, compact)
    _warn_if_stale(payload)


def _resolve_nation(args, backend: Backend) -> str:
    nation = args.me or os.environ.get("WB_NATION") or ""
    if not nation:
        index = backend.index()
        names = [k["名称"] for k in (index.get("kingdoms") or [])]
        print("没有指定国家。请用 --me <国名>，或设置环境变量 WB_NATION。", file=sys.stderr)
        if names:
            print("当前世界里的国家：" + "、".join(names), file=sys.stderr)
        raise SystemExit(2)
    return nation


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------

def cmd_whoami(args, backend: Backend) -> int:
    index = backend.index()
    if not index:
        print("读不到列国索引。请先启动 `python worldbox_agent.py serve`，"
              "或先运行 `python worldbox_agent.py export`。", file=sys.stderr)
        return 2
    if args.nation:
        kid = _slug_key(index, args.nation)
        if kid is None:
            print(f"没有找到叫 {args.nation!r} 的国家。", file=sys.stderr)
            return 2
        for k in index.get("kingdoms", []):
            if str(k["id"]) == kid:
                folder = k.get("目录")
                _emit({**k, "情报目录": f"{folder}/self.json"}, args.compact)
                return 0
        return 2
    print(f"世界: {index.get('世界名称')}   第 {index.get('年份')} 年   "
          f"数据 {index.get('存档距今秒数')} 秒前")
    print(f"后端: {backend.mode}")
    for k in index.get("kingdoms", []):
        alive = "存续" if k.get("是否存续") else "已灭亡"
        print(f"  #{k['id']:<3} {k['名称']:<16} {alive}  人口={k['人口']:<6} "
              f"城市={k['城市数']:<3} 军队={k['军队兵力']:<5} 声望={k['声望']}")
    print("\n用 --me <国名> 指定你扮演的国家。")
    _warn_if_stale(index)
    return 0


def cmd_self(args, backend: Backend) -> int:
    payload = backend.self_view(_resolve_nation(args, backend))
    if payload is None:
        print("读不到本国情报。", file=sys.stderr)
        return 2
    _emit(payload, args.compact)
    return 0


def cmd_others(args, backend: Backend) -> int:
    payload = backend.others(_resolve_nation(args, backend))
    if payload is None:
        print("读不到列国简报。", file=sys.stderr)
        return 2
    _emit(payload, args.compact)
    return 0


def cmd_history(args, backend: Backend) -> int:
    payload = backend.history(_resolve_nation(args, backend), args.table, args.last)
    if payload is None:
        print("读不到历史数据。", file=sys.stderr)
        return 2
    series = payload.get("series") or []
    if args.last:
        payload = dict(payload, series=series[-args.last:])
    _emit(payload, args.compact)
    return 0


def cmd_events(args, backend: Backend) -> int:
    payload = backend.events(_resolve_nation(args, backend), args.last)
    if payload is None:
        print("读不到事件记录。", file=sys.stderr)
        return 2
    _emit(payload, args.compact)
    return 0


def cmd_world(args, backend: Backend) -> int:
    payload = backend.world()
    if not payload:
        print("读不到世界概览。", file=sys.stderr)
        return 2
    _emit(payload, args.compact)
    return 0


def _agent_dir(nation: str) -> Path:
    return DEFAULT_DATA / "agents" / nation


def cmd_persona(args, backend: Backend) -> int:
    """人格档案的落地：跨回合的记忆靠它，第一次运行时最容易漏掉。"""
    nation = args.me or os.environ.get("WB_NATION") or (args.nation or "")
    if not nation:
        print("请用 --me <国名> 指定国家。", file=sys.stderr)
        return 2

    folder = _agent_dir(nation)
    persona = folder / "persona.md"
    memory = folder / "memory.md"

    if not args.init:
        print(f"国家     : {nation}")
        print(f"档案目录 : {folder}")
        print(f"  persona.md : {'存在' if persona.is_file() else '缺失 ← 先建它'}")
        print(f"  memory.md  : {'存在' if memory.is_file() else '缺失 ← 第一次决策后追加'}")
        if not (persona.is_file() and memory.is_file()):
            print("\n这两个文件是领袖跨回合保持同一个人格的依据。"
                  "\n没有它们，每次决策都会像换了一个国王。")
            print(f"运行这个命令初始化：\n  python {Path(__file__).name} persona --me {nation} --init")
        else:
            print(f"\n决策前先读：{persona}\n每回合追加：{memory}")
        return 0

    folder.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    if not persona.is_file():
        template = _load_persona_template()
        persona.write_text(template.replace("<国名>", nation),
                           encoding="utf-8", newline="\n")
        made.append(str(persona))
    if not memory.is_file():
        memory.write_text(
            f"# 领袖记忆 · {nation}\n\n"
            "> 每回合决策后**追加**一节，不要覆盖历史。\n"
            "> 动笔做下一份国策之前，先从头读一遍——\n"
            "> 一个不记得自己承诺过的领袖，不配称为一个角色。\n\n"
            "## 记录格式\n\n"
            "### 第 <年份> 年\n"
            "- **做了什么**：\n"
            "- **结果如何**（下一回合回填）：\n"
            "- **对谁产生了什么情绪**：\n"
            "- **公开说过的承诺**（含期限）：\n"
            "- **我自己怎么看这一年**：\n",
            encoding="utf-8", newline="\n")
        made.append(str(memory))

    if made:
        print("已创建：")
        for path in made:
            print(f"  {path}")
        print("\n接下来：填 persona.md 的『内心』一节，"
              "然后每回合把决策与结果追加到 memory.md。")
    else:
        print(f"档案已存在，未改动：\n  {persona}\n  {memory}")

    existing = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
    print(f"\n目录现在有：{', '.join(existing) or '（空）'}")
    return 0


def _load_persona_template() -> str:
    """优先用 skill 自带的模板，找不到就给一个最小可用骨架。"""
    here = Path(__file__).resolve()
    for base in (here.parent.parent, here.parent.parent.parent / "worldbox-leader-policy"):
        candidate = base / "references" / "领袖档案模板.md"
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError:
                break
    return (
        "# 领袖档案 · <国名>\n\n"
        "## 一、身份\n- 国名 / 编号：\n- 我的名字与称号：\n- 我是一个怎样的君主：\n\n"
        "## 二、游戏的客观事实（从情报里抄，不要编）\n"
        "- 国王特质：\n- 国家特质：\n- 历任国王数：\n\n"
        "## 三、内心\n### 我信什么\n### 我怕什么\n### 我想要什么\n"
        "### 我内心的矛盾\n### 我的说话方式\n\n"
        "## 四、我与其他国家的关系\n\n"
        "## 五、历年大事\n\n"
        "## 六、未兑现的承诺\n- [ ]\n"
    )


def _warn_if_no_persona(nation: str) -> None:
    """档案缺失时提醒——不建也能跑，但每回合都会换个国王。"""
    folder = _agent_dir(nation)
    if (folder / "persona.md").is_file():
        return
    print(
        f"\n提示：{nation} 还没有领袖档案（{folder / 'persona.md'}）。\n"
        f"    没有它，你每次决策都不会记得上次说过什么。\n"
        f"    建议先运行： python {Path(__file__).name} persona --me {nation} --init",
        file=sys.stderr,
    )


def cmd_brief(args, backend: Backend) -> int:
    """最省 token 的摘要：决策真正需要的十几个数字。"""
    nation = _resolve_nation(args, backend)
    _warn_if_no_persona(nation)
    me = backend.self_view(nation)
    if me is None:
        print("读不到本国情报。", file=sys.stderr)
        return 2
    others = backend.others(nation) or {}
    hist = (me.get("history") or [])[-8:]
    span = ([hist[0].get("timestamp"), hist[-1].get("timestamp")]
            if hist else None)

    def trend(field: str) -> Optional[dict]:
        values = [h.get(field) for h in hist if h.get(field) is not None]
        if not values:
            return None
        # 趋势项自带年份区间，否则 agent 无从知道这串数字覆盖多久，
        # 很容易把它误当成全部历史。
        return {"年份区间": span, "数值": values,
                "变化": values[-1] - values[0]}

    last = hist[-1] if hist else {}
    summary = {
        "我": me.get("identity", {}).get("名称"),
        "编号": me.get("identity", {}).get("id"),
        "存续": me.get("status", {}).get("是否存续"),
        "数据新鲜度": me.get("world", {}),
        "国王": (me.get("ruler") or {}).get("国王"),
        "国王特质": [t.get("label") for t in (me.get("ruler") or {}).get("国王特质", [])],
        "国家特质": [t.get("label") for t in (me.get("status") or {}).get("国家特质", [])],
        "人口": (me.get("demographics") or {}).get("人口"),
        "军队": (me.get("military") or {}).get("军队总兵力"),
        "城市数": (me.get("territory") or {}).get("城市数"),
        "领土地块": (me.get("territory") or {}).get("领土地块"),
        "声望_即时": (me.get("status") or {}).get("声望"),
        "声望_最新统计年": last.get("renown"),
        "声望排名": (me.get("status") or {}).get("声望排名"),
        "粮食": last.get("food"),
        "国库": last.get("money"),
        "饥饿人数": (me.get("demographics") or {}).get("饥饿"),
        "趋势_人口_不含船只": trend("population"),
        "趋势_军队_即时统计": trend("army"),
        "趋势_国库": trend("money"),
        "趋势_领土地块": trend("territory"),
        "战争": [
            {"对手": [o["名称"] for o in w.get("敌方", [])],
             "我方角色": w.get("我方角色"),
             "我方阵亡": w.get("我方阵亡"),
             "敌方阵亡": w.get("敌方阵亡")}
            for w in (me.get("diplomacy") or {}).get("战争", [])
        ],
        "其他国家": [
            {"名称": n.get("名称"), "立场": n.get("我方立场"),
             "人口": n.get("人口"), "军队": n.get("军队兵力"),
             "城市数": n.get("城市数"), "声望": n.get("声望")}
            for n in (others.get("列国") or [])
        ],
        "近期大事": [
            {"年份": e.get("年份"), "事件": e.get("事件")}
            for e in (me.get("recent_events") or [])[-6:]
        ],
        "_读法": "趋势项的『年份区间』说明这串数字覆盖哪几年；"
                "『声望_即时』来自最新存档，『声望_最新统计年』来自游戏逐年统计，"
                "两者时间点不同、都真实。",
    }
    _emit(summary, args.compact)
    return 0


def cmd_raw(args, backend: Backend) -> int:
    payload = backend.raw(args.path)
    if payload is None:
        print("原始接口只能在后端为 http 时使用。", file=sys.stderr)
        return 2
    _emit(payload, args.compact)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wb",
        description="WorldBox 国家情报查询（只读）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # --me / --compact 同时在顶层和每个子命令上注册，这样两种写法都能用：
    #     wb --me 大仁 brief      wb brief --me 大仁
    # 子命令上的默认值用 SUPPRESS，避免它把顶层已经解析到的值覆盖掉。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--me", default=None,
                        help="你扮演的国家（也可用环境变量 WB_NATION）")
    common.add_argument("--compact", action="store_true",
                        help="输出单行 JSON（省 token）")

    p.add_argument("--me", default=None,
                   help="你扮演的国家（也可用环境变量 WB_NATION）")
    p.add_argument("--compact", action="store_true", help="输出单行 JSON（省 token）")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("whoami", help="我是谁 / 列出所有国家", parents=[common])
    s.add_argument("nation", nargs="?", help="可选：确认某个国名")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("self", help="本国完整国情", parents=[common])
    s.set_defaults(func=cmd_self)

    s = sub.add_parser("others", help="其他国家的横向对比", parents=[common])
    s.set_defaults(func=cmd_others)

    s = sub.add_parser("history", help="本国逐年趋势", parents=[common])
    s.add_argument("--last", type=int, default=20, help="只看最近 N 年（默认 20）")
    s.add_argument("--table", default="KingdomYearly1",
                   choices=["KingdomYearly1", "KingdomYearly10", "KingdomYearly100"])
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("events", help="本国大事记", parents=[common])
    s.add_argument("--last", type=int, default=20, help="只看最近 N 条（默认 20）")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("world", help="世界概览与国力排行", parents=[common])
    s.set_defaults(func=cmd_world)

    s = sub.add_parser("persona", help="查看/初始化领袖人格档案（跨回合记忆）",
                       parents=[common])
    s.add_argument("nation", nargs="?", help="国名（也可用 --me）")
    s.add_argument("--init", action="store_true",
                   help="创建 persona.md 与 memory.md（已存在则不覆盖）")
    s.set_defaults(func=cmd_persona)

    s = sub.add_parser("brief", help="极简摘要（推荐每次决策前先用它）", parents=[common])
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("raw", help="直接请求某个接口路径，如 raw nations", parents=[common])
    s.add_argument("path", help="例如 nations 或 /api/v1/world")
    s.set_defaults(func=cmd_raw)

    # 子命令上的默认值不能覆盖顶层已解析的值
    for sp in sub.choices.values():
        for act in sp._actions:
            if act.dest in ("me", "compact"):
                act.default = argparse.SUPPRESS
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    backend = Backend()
    try:
        return int(args.func(args, backend) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
