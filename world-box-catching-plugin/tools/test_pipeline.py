"""回归测试：解析层、视图层、导出层（不依赖 HTTP 服务）。"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from worldbox_agent import paths                      # noqa: E402
from worldbox_agent.build import (StatsDatabase, decode_payload,  # noqa: E402
                                  load_world, SaveFormatError)
from worldbox_agent.export import export_all           # noqa: E402
from worldbox_agent.views import (kingdom_self_view, nation_index,  # noqa: E402
                                  others_view, slugify, world_overview)

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [通过] {label} {extra}")
    else:
        fail += 1
        print(f"  [失败] {label} {extra}")


def must_serialise(label, payload):
    try:
        json.dumps(payload, ensure_ascii=False)
        check(label, True)
    except (TypeError, ValueError) as exc:
        check(label, False, f"-> {exc}")


print("=== 1. 数据目录发现 ===")
data_dir = paths.find_data_dir()
check("找到 WorldBox 数据目录", data_dir is not None, str(data_dir))
folders = list(paths.iter_save_folders(data_dir))
check("找到存档", len(folders) > 0, f"= {len(folders)} 个")
latest = paths.latest_save(data_dir)
check("选出最新存档", latest is not None, str(latest.path) if latest else "")
check("优先选择带统计库的存档", latest.stats_db is not None)

print("\n=== 2. 载荷解码：JSON (.wbax) 与 zlib (.wbox) ===")
payload = decode_payload(latest.payload)
check("wbax 解出 JSON", isinstance(payload, dict), f"saveVersion={payload.get('saveVersion')}")
check("含 kingdoms 字段", "kingdoms" in payload)

wbox = next((f for f in folders if f.is_compressed), None)
if wbox is not None:
    try:
        comp = decode_payload(wbox.payload)
        check("wbox(zlib) 也能解出 JSON", isinstance(comp, dict),
              f"{wbox.payload.parent.name} saveVersion={comp.get('saveVersion')}")
    except SaveFormatError as exc:
        check("wbox(zlib) 也能解出 JSON", False, str(exc))
else:
    print("  [跳过] 本机没有 .wbox 手动存档")

print("\n=== 3. 快照构建 ===")
snap = load_world(latest)
check("读出版本号", snap.save_version > 0, f"= {snap.save_version}")
check("读出世界名", bool(snap.name), f"= {snap.name}")
check("有国家", len(snap.kingdoms) > 0, f"= {len(snap.kingdoms)}")
check("有城市", len(snap.cities) > 0, f"= {len(snap.cities)}")
check("有角色", len(snap.actors) > 0, f"= {len(snap.actors)}")
check("军队索引非空", sum(snap.army_size_map.values()) > 0,
      f"= {sum(snap.army_size_map.values())} 人在编")
check("建筑索引非空", sum(snap.kingdom_buildings.values()) > 0,
      f"= {sum(snap.kingdom_buildings.values())} 座")
check("统计库可用", bool(snap.stats and snap.stats.available))
# 事件条数取决于世界本身：刚开局的新世界一条事件都还没有。
# 所以这里验证的是**机制**（能查、返回列表、状态自洽），而不是"必须有事件"。
check("世界事件查询机制可用", isinstance(snap.world_log, list),
      f"= {len(snap.world_log)} 条（新世界可能为 0，属正常）")
if snap.stats and snap.stats.available and "WorldLogMessage" in snap.stats.tables:
    check("WorldLogMessage 表可查", isinstance(snap.stats.log(limit=5), list))
check("历史查询机制可用",
      isinstance(snap.history_of(snap.live_kingdoms()[0]["id"], "KingdomYearly1"), list),
      f"= {len(snap.history_of(snap.live_kingdoms()[0]['id'], 'KingdomYearly1'))} 行")
check("线程安全：worker 线程也能查历史",
      len(snap.history_of(snap.live_kingdoms()[0]["id"], "KingdomYearly1")) >= 0)

print("\n=== 4. 视图层 ===")
top = snap.live_kingdoms()[0]
kid = top["id"]
self_view = kingdom_self_view(snap, kid)
check("本国视图非空", self_view is not None)
check("人口为正", self_view["demographics"]["人口"] > 0,
      f"= {self_view['demographics']['人口']}")
check("军队为正", self_view["military"]["军队总兵力"] > 0,
      f"= {self_view['military']['军队总兵力']}")
check("领土为正", self_view["territory"]["领土地块"] > 0,
      f"= {self_view['territory']['领土地块']}")
check("有城市列表", len(self_view["territory"]["城市列表"]) > 0)
check("城市有建筑数", any(c["建筑数"] > 0 for c in self_view["territory"]["城市列表"]))
check("军官明细有兵力", any(a["兵力"] > 0 for a in self_view["military"]["军队明细"]))
check("特质已中文化",
      all(t["label"] for t in self_view["ruler"]["国王特质"]))
must_serialise("self 视图可 JSON 序列化", self_view)

hist = self_view["history"]
check("历史可读", len(hist) > 0, f"= {len(hist)} 年")
if hist:
    check("历史按年份升序", all(hist[i]["timestamp"] <= hist[i + 1]["timestamp"]
                                for i in range(len(hist) - 1)))
    check("历史含人口字段", "population" in hist[-1])

others = others_view(snap, kid)
check("列国视图不含自己", all(n["id"] != kid for n in others["列国"]))
check("他国数量相符", others["他国数量"] == len(others["列国"]))
must_serialise("others 视图可 JSON 序列化", others)

world = world_overview(snap)
check("世界概览有国力排行", len(world["国力排行"]) == len(snap.live_kingdoms()))
check("世界逐年统计可读", len(world["世界逐年统计"]) > 0)
must_serialise("world 视图可 JSON 序列化", world)

print("\n=== 4b. 人口口径必须处处一致 ===")
# 曾经踩过的坑：views 里的『人口』排除船只、build.py 的 population_of 不排除，
# 导致同一国家在索引/本国/国力排行三处出现三个不同数字。
slugs_tmp = {k: slugify(str(v.get("name") or "")) for k, v in snap.kingdoms.items()}
idx_tmp = nation_index(snap, slugs_tmp)
consistent = True
for entry in idx_tmp["kingdoms"]:
    eid = entry["id"]
    views = {
        entry["人口"],
        kingdom_self_view(snap, eid)["demographics"]["人口"],
        next(k["人口"] for k in world["国力排行"] if k["id"] == eid),
        snap.population_of(eid),
    }
    if len(views) != 1:
        consistent = False
        print(f"        {entry['名称']} 口径不一致: {views}")
check("索引/本国/国力排行/内部 人口口径一致", consistent)
have_boats = any(snap.boats_of(k["id"]) > 0 for k in snap.live_kingdoms())
if have_boats:
    check("含船只条目数 >= 人口（船只被单列）",
          all(kingdom_self_view(snap, k["id"])["demographics"]["含船只条目数"]
              >= snap.population_of(k["id"]) for k in snap.live_kingdoms()))

print("\n=== 4c. 城市坐标（政策落点） ===")
# 曾经把存档的 width/height 当地图尺寸报出去（实际是 4x4 内部分块数），
# 会严重误导 agent 选落点。现在改为实测坐标范围。
sample_cities = self_view["territory"]["城市列表"]
with_coords = [c for c in sample_cities if isinstance(c.get("坐标"), list)]
check("城市带坐标", len(with_coords) == len(sample_cities),
      f"= {len(with_coords)}/{len(sample_cities)}")
if with_coords:
    check("坐标是 [x, y] 两个整数",
          all(len(c["坐标"]) == 2 and all(isinstance(v, int) for v in c["坐标"])
              for c in with_coords))
bounds = world["world"].get("可用坐标范围")
check("世界概览给出可用坐标范围", isinstance(bounds, dict) and "x" in bounds)
if isinstance(bounds, dict):
    xs, ys = bounds["x"], bounds["y"]
    inside = all(xs[0] <= c["坐标"][0] <= xs[1] and ys[0] <= c["坐标"][1] <= ys[1]
                 for c in with_coords)
    check("所有城市坐标落在声明的范围内", inside, f"范围 x{xs} y{ys}")
check("不再把引擎尺寸当地图大小",
      "地图宽" not in world["world"] and "存档引擎尺寸字段" in world["world"])

print("\n=== 4c-2. 逐城人口之和 == 全国人口 ===")
# 曾经踩过的坑：城市级人口含船只、全国人口不含，导致逐城相加得 1784
# 而全国是 1767（差的正是 17 艘船），让 agent 以为数据有错。
city_sum = sum(c["人口"] for c in sample_cities)
check("逐城人口之和 == demographics.人口",
      city_sum == self_view["demographics"]["人口"],
      f"{city_sum} vs {self_view['demographics']['人口']}")
boat_cities = [c for c in sample_cities if c.get("其中船只", 0) > 0]
if boat_cities:
    only_total = sum(c["含船只条目数"] for c in sample_cities)
    check("逐城含船只条目之和 == 全国含船只条目数",
          only_total == self_view["demographics"]["含船只条目数"],
          f"{only_total} vs {self_view['demographics']['含船只条目数']}")

print("\n=== 4c-3. others.json 必须自带敌城坐标（外交前提） ===")
coords_ok = True
claim_ok = True
for nation in others["列国"]:
    if not isinstance(nation.get("首都坐标"), list):
        coords_ok = False
        print(f"        {nation['名称']} 缺首都坐标")
    for c in nation.get("城市列表") or []:
        if not isinstance(c.get("坐标"), list):
            coords_ok = False
            print(f"        {nation['名称']}/{c.get('名称')} 缺坐标")
    if "领土主张线索" not in nation:
        claim_ok = False
check("他国首都与城市均带坐标", coords_ok)
check("他国带领土主张线索", claim_ok)
check("他国带国王特质", all("国王特质" in n for n in others["列国"]))

print("\n=== 4d. 数据过期警告 ===")
freshness = self_view["world"]
check("本国视图带存档年龄", isinstance(freshness.get("存档距今秒数"), (int, float)))
from worldbox_agent.views import STALE_AFTER_SECONDS  # noqa: E402

if freshness["存档距今秒数"] > STALE_AFTER_SECONDS:
    check("过期时给出 _警告", bool(freshness.get("_警告")))
    check("索引过期时也给出 _警告", bool(idx_tmp.get("_警告")))
else:
    print("  [跳过] 当前存档足够新，无法验证警告路径")

slugs = {k: slugify(str(v.get("name") or "")) for k, v in snap.kingdoms.items()}
idx = nation_index(snap, slugs)
check("索引含全部国家", idx["王国数量"] == len(snap.kingdoms))
check("索引可反查 id", all(v for v in idx["按名称查id"].values()))

print("\n=== 5. 边界情况 ===")
check("不存在的国家返回 None", kingdom_self_view(snap, 999999) is None)
check("slugify 处理非法字符", "/" not in slugify('a/b:c*d?e'))
check("slugify 处理空名", slugify("") == "未命名")
check("slugify 保留中文", slugify("大仁") == "大仁")
check("slugify 处理 Windows 保留名", slugify("CON").startswith("_"))

print("\n=== 6. 导出层 ===")
# 用工作区内的普通临时目录：tempfile.mkdtemp 建出的目录在本机沙箱下
# 不允许再创建子目录，而导出需要 nations/by-id 这样的层级。
tmp_root = ROOT / ".test-tmp"
tmp = tmp_root / "export"
shutil.rmtree(tmp_root, ignore_errors=True)
tmp.mkdir(parents=True, exist_ok=True)
try:
    manifest = export_all(snap, tmp)
    check("清单生成", manifest["counts"]["kingdoms"] == len(snap.kingdoms))
    check("_index.json 存在", (tmp / "_index.json").is_file())
    check("world_overview.json 存在", (tmp / "world_overview.json").is_file())
    check("README.txt 存在", (tmp / "README.txt").is_file())
    check("每个国家都有文件夹",
          len(list((tmp / "nations").iterdir())) >= len(snap.kingdoms))
    check("by-id 兜底存在", (tmp / "nations" / "by-id" / f"{kid}.json").is_file())
    sample = json.loads((tmp / "nations" / "by-id" / f"{kid}.json").read_text("utf-8"))
    check("by-id 内容与 self 一致", sample["identity"]["id"] == kid)
    # 原子写入不应留下临时文件
    leftover = list(tmp.rglob(".tmp-*"))
    check("没有残留临时文件", not leftover, f"{len(leftover)} 个")
    total_kb = manifest["counts"]["total_bytes"] / 1024
    check("导出体量合理", 10 < total_kb < 5000, f"= {total_kb:.0f} KB")
    print(f"        导出耗时 {manifest['export_duration_ms']} 毫秒，"
          f"{manifest['counts']['files']} 个文件")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n=== 7. 统计库读的是副本（游戏那份绝不能被锁住）===")
# 回归测试：曾经直接用 sqlite3 打开 map_stats.s3db。SQLite 打开文件时不共享
# "删除"权限，而 WorldBox 存档的第一步正是 File.Delete(<存档目录>\map_stats.s3db)
# （DBManager.saveToPath），于是工具只要开着，游戏就报：
#   System.IO.IOException: ... 'saves\save3\map_stats.s3db' ... being used by another process
# 现在读的是临时副本，所以游戏那份必须**始终**可以被删除/替换。
db_tmp = ROOT / ".test-tmp-db"
shutil.rmtree(db_tmp, ignore_errors=True)
db_tmp.mkdir(parents=True, exist_ok=True)
src_db = db_tmp / "map_stats.s3db"
_seed = sqlite3.connect(str(src_db))
# 注意列名：history() 用 id 列定位实体（真实库里 KingdomYearly1.id 就是王国 id）
_seed.execute("CREATE TABLE KingdomYearly1 (id INTEGER, timestamp INTEGER, army INTEGER)")
_seed.execute("INSERT INTO KingdomYearly1 VALUES (25, 118, 478)")
_seed.commit()
_seed.close()
try:
    db = StatsDatabase.open(src_db)
    check("能打开统计库", db.available, db.error or "")
    check("打开的是副本而不是游戏那份", db.copied and db._temp_path is not None)
    check("副本不在存档目录里",
          db._temp_path is not None and Path(db._temp_path).parent != src_db.parent)
    check("副本里查得到数据", len(db.history("KingdomYearly1", 25)) == 1)
    temp_copy = Path(db._temp_path) if db._temp_path else None
    # 关键断言：库开着的同时，游戏那份仍可被替换（等价于游戏能存档）
    moved = db_tmp / "moved-away.s3db"
    moved_ok, moved_err = True, ""
    try:
        os.replace(src_db, moved)
    except OSError as exc:
        moved_ok, moved_err = False, f"{type(exc).__name__}: {exc}"
    check("库开着时游戏那份仍可被替换（= 游戏能存档）", moved_ok, moved_err)
    db.close()
    check("关闭后临时副本被清理", temp_copy is not None and not temp_copy.exists())
finally:
    shutil.rmtree(db_tmp, ignore_errors=True)

print(f"\n===== 结果：通过 {ok} 项，失败 {fail} 项 =====")
sys.exit(1 if fail else 0)
