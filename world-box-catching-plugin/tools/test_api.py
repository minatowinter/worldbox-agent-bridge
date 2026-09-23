import json, sys, urllib.parse, urllib.request, urllib.error

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8795")
ok = 0
fail = 0


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [通过] {label} {extra}")
    else:
        fail += 1
        print(f"  [失败] {label} {extra}")


print("=== 1. 列国索引 /api/v1/nations ===")
idx = get("/api/v1/nations")
print(f"  世界={idx['世界名称']} 年份={idx['年份']} 王国数={idx['王国数量']}")
for k in idx["kingdoms"]:
    print(f"    #{k['id']:<3} {k['名称']:<12} 人口={k['人口']:<5} 城市={k['城市数']:<3} "
          f"军队={k['军队兵力']:<4} 声望={k['声望']:<5} -> {k['目录']}")
check("索引含全部国家", idx["王国数量"] == len(idx["kingdoms"]))
check("名称查 id 可用", len(idx["按名称查id"]) >= 1)

name = idx["kingdoms"][0]["名称"]
enc = urllib.parse.quote(name)

print(f"\n=== 2. 本国情报 /api/v1/nations/{name} ===")
s = get(f"/api/v1/nations/{enc}")
print(f"  名称={s['identity']['名称']} 格言={s['identity']['格言']} 种族={s['identity']['种族称呼']}")
print(f"  国王={s['ruler']['国王']} 特质={[t['label'] for t in s['ruler']['国王特质']]}")
print(f"  人口={s['demographics']['人口']} 成年={s['demographics']['成年人口']} "
      f"儿童={s['demographics']['儿童人口']} 军队={s['military']['军队总兵力']}")
print(f"  城市={s['territory']['城市数']} 地块={s['territory']['领土地块']} "
      f"建筑={s['territory']['建筑总数']}")
print(f"  职业分布={[(p['职业'], p['人数']) for p in s['demographics']['职业分布']]}")
print(f"  外交={[(r['名称'], r['关系']) for r in s['diplomacy']['对外关系']]}")
print(f"  历史行数={len(s['history'])} 可用={s['history_meta']['可用']}")
if s["history"]:
    last = s["history"][-1]
    print(f"  最近一年: 年={last.get('timestamp')} 人口={last.get('population')} "
          f"军队={last.get('army')} 国库={last.get('money')} 领土={last.get('territory')}")
print(f"  近期事件={[(e['年份'], e['事件']) for e in s['recent_events']][-5:]}")
check("本国人口 > 0", s["demographics"]["人口"] > 0)
check("军队已统计", s["military"]["军队总兵力"] > 0, f"= {s['military']['军队总兵力']}")
check("历史非空", len(s["history"]) > 0, f"= {len(s['history'])} 行")
check("成年人口已从历史取得", s["demographics"]["成年人口"] is not None)
check("建筑数 > 0", s["territory"]["建筑总数"] > 0)
# 事件条数取决于世界本身：刚开局的新世界可能一条都还没有。
# 这里验证机制（字段是列表、结构正确），而不是"必须有事件"。
check("本国事件结构正确", isinstance(s["recent_events"], list),
      f"= {len(s['recent_events'])} 条（新世界可能为 0）")
if s["recent_events"]:
    check("事件字段完整",
          all("年份" in e and "事件" in e for e in s["recent_events"]))

print(f"\n=== 3. 列国简报 /api/v1/nations/{name}/others ===")
o = get(f"/api/v1/nations/{enc}/others")
for n in o["列国"]:
    print(f"    {n['名称']:<12} 立场={n['我方立场']:<4} 军队={n['军队兵力']:<4} "
          f"人口={n['人口']:<5} 城市={n['城市数']}")
print(f"  关系矩阵={o['关系矩阵']}")
check("他国数量正确", o["他国数量"] == len(o["列国"]))

print("\n=== 4. 历史分辨率 /api/v1/nations/6/history?history=KingdomYearly10 ===")
h = get("/api/v1/nations/6/history?history=KingdomYearly10")
check("十年表可读", isinstance(h["series"], list), f"= {len(h['series'])} 行")

print("\n=== 5. 某国大事记 /api/v1/nations/6/events ===")
e = get("/api/v1/nations/6/events")
check("事件接口可用", isinstance(e["events"], list), f"= {len(e['events'])} 条")

print("\n=== 6. 世界概览 /api/v1/world ===")
w = get("/api/v1/world")
print(f"  世界={w['world']['名称']} 年份={w['world']['年份']} 纪元={w['world']['纪元']}")
print(f"  国力排行={[(k['名称'], k['人口'], k['军队兵力']) for k in w['国力排行']]}")
print(f"  逐年统计={len(w['世界逐年统计'])} 行  全局事件={len(w['全局事件记录'])} 条")
check("世界逐年统计非空", len(w["世界逐年统计"]) > 0, f"= {len(w['世界逐年统计'])}")
check("统计库可用", w["统计数据源"]["可用"] is True)

print("\n=== 7. 全球事件 /api/v1/events?limit=5 ===")
ev = get("/api/v1/events?limit=5")
check("全球事件接口可用", isinstance(ev["events"], list),
      f"= {len(ev['events'])} 条（新世界可能为 0）")
check("事件接口返回自洽的元信息", "count" in ev and "kingdom_id" in ev)

print("\n=== 8. 错误处理 ===")
for bad in ["/api/v1/nations/" + urllib.parse.quote("不存在的国家"), "/api/v1/nope", "/api/v9/world"]:
    try:
        get(bad)
        check(f"{bad} 应报错", False)
    except urllib.error.HTTPError as ex:
        body = json.loads(ex.read().decode("utf-8"))
        check(f"{bad} -> HTTP {ex.code}", ex.code in (400, 404), body.get("error", "")[:40])

print(f"\n===== 结果：通过 {ok} 项，失败 {fail} 项 =====")
sys.exit(1 if fail else 0)
