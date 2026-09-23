"""存档发现/选择/持久化的回归测试。

背景：一个正常玩家会有几十个存档，分属不同世界与时间（本机实测两组相差
38 天、世界名都不同）。工具若永远只取"最新"，多 agent 房间就会被接到一个
使用者不关心的世界上。所以"用哪份存档"必须可显式选择、可记忆、可恢复默认。
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from worldbox_agent import paths  # noqa: E402

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [通过] {label} {extra}")
    else:
        fail += 1
        print(f"  [失败] {label} {extra}")


data_dir = paths.find_data_dir()
check("找到 WorldBox 数据目录", data_dir is not None, str(data_dir))

print("\n=== 1. 存档清单 ===")
rows = paths.list_saves(data_dir)
check("列得出存档", len(rows) > 0, f"= {len(rows)} 份")
if not rows:
    print("\n没有存档，无法继续测试。")
    sys.exit(2)

first = rows[0]
for field in ("key", "index", "kind_label", "time_local", "age_text",
              "world_name", "has_stats", "size_mb", "stale", "selected"):
    check(f"清单含字段 {field}", field in first)
check("按时间倒序（最新在前）",
      all(rows[i]["timestamp"] >= rows[i + 1]["timestamp"] for i in range(len(rows) - 1)))
check("序号从 1 开始", rows[0]["index"] == 1)
check("世界名不依赖解析大文件", isinstance(first["world_name"], str),
      f"= {first['world_name']!r}")

stale_count = sum(1 for r in rows if r["stale"])
print(f"        共 {len(rows)} 份，其中 {stale_count} 份已过期（>10 分钟）")
kinds = {r["kind_label"] for r in rows}
print(f"        类型: {', '.join(sorted(kinds))}")

print("\n=== 2. 各种选择写法都能解析 ===")
latest = paths.resolve_save(data_dir, None)
check("None → 有结果", latest is not None, f"= {latest.key}")
check("latest 关键字", paths.resolve_save(data_dir, "latest").key == latest.key)
check("#1 序号", paths.resolve_save(data_dir, "#1").key == rows[0]["key"])
if len(rows) > 2:
    check("#3 序号", paths.resolve_save(data_dir, "#3").key == rows[2]["key"],
          f"= {rows[2]['key']}")
check("纯数字序号", paths.resolve_save(data_dir, "1").key == rows[0]["key"])
check("精确文件夹名", paths.resolve_save(data_dir, rows[0]["key"]).key == rows[0]["key"])
check("完整路径", paths.resolve_save(data_dir, str(rows[0]["path"])).key == rows[0]["key"])
name = first["world_name"]
if name:
    hit = paths.resolve_save(data_dir, name[:6])
    check("按世界名子串", hit is not None and hit.world_name() == name,
          f"{name[:6]!r} -> {hit.key if hit else None}")
check("#999 越界 → None",
      paths.resolve_save(data_dir, "#999", fallback_to_latest=False) is None)
check("乱写且不回退 → None",
      paths.resolve_save(data_dir, "zzz不存在zzz", fallback_to_latest=False) is None)
check("乱写但回退 → 最新",
      paths.resolve_save(data_dir, "zzz不存在zzz").key == latest.key)

print("\n=== 3. 选择的持久化 ===")
tmp = ROOT / ".test-tmp" / "state"
tmp.mkdir(parents=True, exist_ok=True)
target = rows[min(3, len(rows) - 1)]["key"]

wrote = paths.save_selection(data_dir, target, fallback=tmp)
check("写入成功", wrote is True)
check("读回一致", paths.load_selection(data_dir, tmp) == target)
resolved = paths.resolve_save(data_dir, None, state_fallback=tmp)
check("无参解析用上了记忆的选择", resolved is not None and resolved.key == target,
      f"{resolved.key if resolved else None} vs {target}")

wrote = paths.save_selection(data_dir, None, fallback=tmp)
check("清空成功", wrote is True)
check("清空后读回为 None", paths.load_selection(data_dir, tmp) is None)
resolved = paths.resolve_save(data_dir, None, state_fallback=tmp)
check("清空后回到最新", resolved is not None and resolved.key == latest.key,
      f"= {resolved.key if resolved else None}")

print("\n=== 4. 不可写目录要回退，而不是静默失败 ===")
readonly = Path("Z:/definitely-not-writable") if sys.platform == "win32" else Path("/proc/nope")
wrote = paths.save_selection(readonly, "x", fallback=tmp)
check("主位置不可写时回退并成功", wrote is True)
check("回退位置确实写了", (tmp / paths.STATE_FILENAME).is_file())
check("回退位置可读回", paths.load_selection(readonly, tmp) == "x")
paths.save_selection(readonly, None, fallback=tmp)

print("\n=== 5. 存档描述不解析大文件（性能） ===")
import time  # noqa: E402

t0 = time.time()
for _ in range(3):
    paths.list_saves(data_dir)
elapsed = (time.time() - t0) / 3
check("列 31 份存档 < 300ms", elapsed < 0.3,
      f"= {elapsed * 1000:.0f} ms（只读 map.meta）")

print("\n=== 6. 边界 ===")
check("空目录不炸", paths.list_saves(ROOT / ".test-tmp" / "empty-missing") == [])
check("find_save 精确命中", (paths.find_save(data_dir, rows[0]["key"]) or None) is not None)
check("find_save 乱名返回 None", paths.find_save(data_dir, "zzz") is None)

import shutil  # noqa: E402

shutil.rmtree(ROOT / ".test-tmp", ignore_errors=True)

print(f"\n===== 结果：通过 {ok} 项，失败 {fail} 项 =====")
sys.exit(1 if fail else 0)
