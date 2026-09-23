# WorldBox 世界接入文档（给"房间程序"开发者）

你正在做的房间程序是一个**多 agent 对话平台**，要让多个 agent 各扮演 WorldBox 里的
一个国家的领袖，在同一条世界频道里对话、做决策。

本文档说明三件事：**有哪些数据可用**、**两个 skill 是什么**、
**平台该怎么把它们接起来**。看完你应该能直接动手。

> 配套代码：`integration/world_connector.py`（纯标准库，可直接 import 或复制）。
> 它就是本文档的参考实现，包含节拍器、上下文压缩、政策解析、动作账本。

---

## 一、先理解三个硬约束

这三条决定了平台架构，不能绕开。**先说清楚，能省掉你后面很多返工。**

### 1. agent 不能直接改变游戏，它只能"提案"

真正在游戏里动手的是**创世者**（玩家本人）。你提的"发展农业"，
实际落地是玩家在某个城镇放置食物。

所以平台必须有**闭环**：

```
agent 提出政策 → 平台收集成"待执行清单" → 玩家照着在游戏里做
      ↑                                              ↓
      └──── 下一轮把"已执行/被拒绝"告诉 agent ←───────┘
```

**没有这个回填，agent 会觉得自己在对着空气说话**，几轮之后它就不再认真提政策了。
`ActionJournal` 就是干这个的。

### 2. 世界的节拍是"存档"，不是你的循环

WorldBox 约**每 5 分钟**自动存档一次。外置工具读的就是这个存档。

这意味着：**你的平台跑得再快，世界也不会变快。**
如果世界没推进你就又开一轮决策，agent 会在**同一帧数据**上反复决策、
反复提出同一条政策——这会迅速毁掉对话质量。

所以平台的主循环必须**由世界推进驱动**：

```
指纹 = f(存档路径, 存档时间戳)
指纹没变 → 世界没变 → 不开新一轮决策（可以让 agent 继续对话，但不决策）
指纹变了 → 世界推进了 → 开新一轮
```

`WorldConnector.status().fingerprint` 与 `has_advanced()` 直接给你这个。

想要世界走得快，是**玩家在游戏里手动多存几次档**，不是调你的循环频率。

### 3. 数据有延迟，而且必须如实告诉 agent

存档距今通常 0–300 秒，游戏暂停时可能几小时。**每个 agent 的上下文里都必须带
这个数字**，超过 600 秒还要带警告。不这样做，agent 会把 3 小时前的战报
当实时局势来决策——而它完全没有能力自己发现这一点。

---

## 二、数据从哪来（三种访问方式）

| 方式 | 适合 | 说明 |
|---|---|---|
| **HTTP 接口** | ✅ 平台首选 | `python worldbox_agent.py serve`，默认 `127.0.0.1:8777` |
| **读磁盘 JSON** | 服务没开时的回退 | `data/` 目录里每个国家一份，`_index.json` 是索引 |
| **命令行 `wb.py`** | 给 agent 自己用，或调试 | 见第四节 |

`WorldConnector` 会**自动探测**：先试 HTTP，失败则读磁盘。
所以**用户忘了启动服务时你的平台不会崩**。

### 目录约定

```
<worldbox-agent 目录>/
├── data/                              ← 快照输出（平台读这里做回退）
│   ├── _index.json                    ★ 列国索引：国名 → 编号 → 目录
│   ├── world_overview.json            世界概览、国力排行、全球战争
│   ├── nations/
│   │   ├── by-id/6.json               按编号取，不必先查名字
│   │   └── 大仁/{self,others,history,events}.json
│   └── agents/大仁/{persona,memory}.md  ← agent 自己写的人格档案
├── integration/world_connector.py      ← 本文档的适配器
└── skills/…                            ← 两个 skill
```

### 关键接口速查

| 接口 | 内容 | 体积 |
|---|---|---|
| `GET /health` | 存活、**世界指纹**、新鲜度 | 1 KB |
| `GET /api/v1/nations` | 列国索引（国名↔编号） | 1.4 KB |
| `GET /api/v1/world` | 世界概览、国力排行、坐标范围 | 13 KB |
| `GET /api/v1/nations/<编号或国名>` | 一国完整国情 | **27 KB** |
| `GET /api/v1/nations/<id>/others` | 列国简报（含**敌城坐标**、领土主张线索） | 4 KB |
| `GET /api/v1/nations/<id>/history` | 逐年趋势 | 3.5 KB |
| `GET /api/v1/nations/<id>/events` | 本国大事记 | 1 KB |
| `GET /api/v1/events` | 全球事件 | — |
| `GET /api/v1/refresh` | 强制重读存档 | — |

`<编号或国名>` 支持 `6`、`#6`、`大仁`、或唯一子串。加 `?compact=1` 去掉缩进。

**注意体积差异**：完整国情 27 KB，**不要每轮直接灌给 agent**。
用 `agent_context()`（见下）能压到 4 KB 左右。

---

## 三、平台怎么接（推荐架构）

```
┌─────────────────── 房间程序 ───────────────────┐
│                                                │
│  WorldConnector                                │
│    ├─ status()          世界指纹 + 新鲜度      │
│    ├─ has_advanced()    ← 节拍器：世界推进了吗 │
│    ├─ agent_context(国) → 4 KB 上下文，注入    │
│    └─ parse_policy_actions()  国策 → 结构化动作│
│                                                │
│  WorldTick 循环                                 │
│    等世界推进 → 为每国构建上下文 → agent 发言  │
│    → 收政策 → 入 ActionJournal → 玩家执行      │
│                                                │
│  ActionJournal（持久化到 data/actions.json）   │
│    proposed → executed / rejected / obsolete   │
│                                                │
│  房间 UI（世界频道）                            │
│    · 每个 agent 的头像/国名/性格               │
│    · 待执行清单（人类照着在游戏里做）          │
│    · 数据新鲜度横幅（过期要显眼）              │
└────────────────────────────────────────────────┘
         ↑ 读                         ↓ 提案
   worldbox_agent 服务           玩家在游戏里执行
         ↑
   WorldBox 写存档（约 /5 分钟）
```

### 3.1 最小可用代码

```python
from integration.world_connector import WorldConnector, ActionJournal

conn = WorldConnector("http://127.0.0.1:8777", data_dir="data")
journal = ActionJournal("data/actions.json")

# 建立"国家 → agent"映射
nations = [n for n in conn.nations() if n["是否存续"]]
agents = {n["名称"]: spawn_agent(n["名称"]) for n in nations}

last = None
while True:
    status = conn.wait_for_advance(last, timeout=900, poll=10)   # 等世界推进
    if status is None:
        continue                      # 超时（游戏可能没开），回到等待
    last = status

    journal.expire_stale_pending(status.fingerprint)   # 上一帧没裁决的提案作废

    # 世界变了 → 让每个国家决策
    for name, agent in agents.items():
        ctx = conn.agent_context(
            name,
            recent_actions=journal.recent_decided(kingdom_id_of(name)),
        )
        reply = agent.say(room="world", context=ctx)     # agent 在世界频道发言

        # 抽出可执行动作，交给玩家
        for act in WorldConnector.parse_policy_actions(
                reply, kingdom_id_of(name), name):
            journal.propose(kingdom_id_of(name), name, act["kind"],
                            act["summary"], target=act["target"],
                            coord=act["coord"], world_year=status.year,
                            fingerprint=status.fingerprint)

        room.broadcast(reply)          # 原文本也留在频道里，供其他 agent 看

    # 把待执行清单交给玩家
    room.show_execution_list(journal.export_execution_list())
```

`spawn_agent` / `room` / `kingdom_id_of` 是你自己的部分。其余都能直接用。

### 3.2 注入给 agent 的上下文长什么样

`agent_context()` 返回的 dict **可以直接 `json.dumps` 塞进消息**。
它就是 agent 这一轮能看到的全部世界：

```json
{
  "角色": "你是 WorldBox 世界「Skulls of Misery」中「大仁」的领袖。",
  "数据新鲜度": {
    "世界年份": 212, "存档距今秒数": 15786.8, "后端": "http",
    "世界指纹": "…\\map.wbax|1789872887.0",
    "警告": "⚠️ 世界数据已 263 分钟旧…"
  },
  "我的国家": {
    "编号": 6, "名称": "大仁", "是否存续": true,
    "国王": "吕䎅冥", "国王特质": ["睿智", "强壮"],
    "国家特质": ["经济强权", "高额贡赋"],
    "人口": 1767, "军队": 309, "城市数": 14, "领土地块": 793,
    "声望": 1623, "声望排名": 1,
    "国库": 23034, "粮食": 8175, "饥饿": 79, "迁出": 952, "迁入": 40,
    "城市列表_按无家可归排序_仅前若干座": [
      { "名称": "北平", "坐标": [4, 23], "人口": 323, "士兵": 60, "无家可归": 83 }
    ]
  },
  "趋势": {
    "人口": { "年份区间": [205, 212], "数值": [2921, 2819, 1896, …], "变化": -1205 },
    "军队": { … }, "国库": { … }, "饥饿": { … }
  },
  "我的战争": [ { "战争": "辛卯革命", "敌方": ["莱国"], "我方阵亡": 71, "敌方阵亡": 153 } ],
  "列国": [ { "名称": "莱国", "立场": "敌对", "军队": 5, "城市列表": [ { "名称": "琼州", "坐标": [5, 3] } ] } ],
  "关系矩阵": { "6|32": "war" },
  "近期大事": [ { "年份": 12468, "事件": "新国王登基" } ],
  "我上一轮提出的政策及结果": [ { "摘要": "在北平增建住房", "状态": "executed",
                                 "创世者回复": "已增建 90 人份" } ],
  "坐标范围": { "x": [1, 254], "y": [1, 254] }
}
```

**为什么是这些字段**：

- `趋势` 是**最关键的**。单点的"人口 1767"什么也说明不了；
  从 2921 掉到 1716 才是一个领袖能据以决策的信息。
  没有趋势，agent 只能描述现状，无法判断国家在崛起还是崩解。
- `城市列表` 只给前几座（按**无家可归**排序，因为那是叛乱前兆且是真实分城数据）。
  大国有几十座城，全列会吃掉几千字节而决策只需要最要紧的几座。
  这是**平台必须做的压缩**——不要指望 agent 自己从 27 KB 里挑。
- `我上一轮提出的政策及结果` 让 agent 知道自己说话有没有用。

### 3.3 待执行清单（给玩家的 UI）

```python
journal.export_execution_list()
# [
#   {"动作": "北平增建/升级住房，容量 +83", "国家": "大仁", "类型": "落点型",
#    "落点坐标": "北平(4,23)", "编号": "a1789…-1"},
#   {"动作": "把「地方高税」下调一档", "国家": "大仁", "类型": "开关型", "编号": "…"}
# ]
```

玩家执行完就回填：

```python
journal.decide("a1789…-1", "executed", "已在北平增建 90 人份住房")
journal.decide("a1789…-2", "rejected", "今年不改税率")
```

下一轮 `agent_context(recent_actions=…)` 会把结果告诉 agent。
**这是整个闭环里最容易漏掉、也最影响体验的一环。**

### 3.4 世界推进后要作废旧提案

```python
journal.expire_stale_pending(status.fingerprint)
```

不这样做的话：世界已经从 212 年推进到 213 年，上一帧"给北平增建住房"的提案
还挂在待执行清单上，而北平的无家可归可能已经被别的因素改变了——
agent 会看到自己针对旧局势的提案还"没人理"，产生错误认知。

---

## 四、两个 skill 是什么

平台本身**不需要**调用 skill——skill 是给参与对话的 agent 读的。
但你需要知道它们的作用，才能设计好注入的上下文和房间的产出格式。

| Skill | 作用 | 何时被触发 |
|---|---|---|
| `worldbox-nation-leader` | **情报**：怎么查自己国家和别国的现状 | agent 需要看数据时 |
| `worldbox-leader-policy` | **政策与人格**：怎么把局势变成可执行操作，并保持领袖形象一致 | agent 要做决策时 |

### 4.1 `worldbox-nation-leader`（情报 skill）

自带一个查询工具 `scripts/wb.py`，agent 可以直接运行：

```bash
python skills/worldbox-nation-leader/scripts/wb.py whoami          # 我是谁
python …/wb.py brief  --me 大仁      # 极简摘要（最省 token）
python …/wb.py self   --me 大仁      # 完整国情
python …/wb.py others --me 大仁      # 列国对比、敌城坐标、领土主张线索
python …/wb.py history --me 大仁 --last 15
python …/wb.py events --me 大仁
python …/wb.py world
python …/wb.py persona --me 大仁          # 领袖人格档案状态
python …/wb.py persona --me 大仁 --init   # 初始化 persona.md / memory.md
```

它还教 agent 几条**必须遵守的纪律**（这些对你的平台很重要）：

- 每个文档都带 `存档距今秒数`；超 600 秒要明确说明数据可能过期
- **绝不编造数据里没有的数字**；字段缺失就说缺失
- 区分"即时值"与"逐年统计值"（两者时点不同，都真实）
- 两套时间刻度（历史纪年 212 / 世界时间 12468）无法换算

### 4.2 `worldbox-leader-policy`（政策与人格 skill）

它规定 agent 输出**固定五节格式**的《国策》：

```
## 第 <年份> 年 · <国名> 国策
### 零、情报状态        ← 数据多旧、缺什么
### 一、局势判断        ← 引用具体数字与趋势
### 二、国策
      A. 落点型措施    ← 执行：请创世者在 北平(4,23) 增建住房，容量 +83
      B. 开关型措施    ← 执行（全国开关）：把「地方高税」下调一档
### 三、对外动作        ← 对他国宣战/求和/结盟；外交文书是这一节的子节
### 四、内心            ← 领袖真实想法（价值观/恐惧/渴望/矛盾）
### 五、待办与追问
```

**这个格式就是你的平台与工具之间的接口。** 两条关键约定：

1. **`A. 落点型`** 必须写成 `…在 <城市名>(x,y) <做什么>，数量 <多少>`——
   `parse_policy_actions()` 靠这个正则抽取坐标。
2. **`B. 开关型`** 必须写成 `执行（全国开关）：…`——它没有坐标，
   是减税/停战/调整法则这类全国政策。**不要逼所有政策都带坐标**，
   否则 agent 会给"减税"编一个坐标，政策就变假了。

### 4.3 人格与跨回合记忆

`worldbox-leader-policy` 要求每个领袖维护两个文件：

```
data/agents/<国名>/persona.md   人格设定（价值观、恐惧、渴望、矛盾、说话方式）
data/agents/<国名>/memory.md    按年追加的记忆（做了什么、结果、对谁的情绪、承诺）
```

**平台应当把这两个文件纳入管理**：

- 建房间时给每个国家 `wb.py persona --me <国名> --init` 初始化
- `persona.md` 的内容**并入该 agent 的 system prompt**（这是它的性格）
- 每轮决策后，把结果追加到 `memory.md`；**下一轮动笔前先让它读**

没有这两个文件，每次决策都会像换了一个国王——领袖形象立不起来，
你的平台也就退化成了"三个 AI 在同一帧数据上各说各话"。

> 工具**不会**覆盖 `data/agents/`——导出只清理 `data/nations/`。
> 但你要注意：如果玩家换了一个世界，这些档案属于旧世界，需要归档或清空。

---

## 五、把工具和平台联结起来

### 5.1 启动顺序

```powershell
# 1) 启动世界数据服务（必须，否则平台只能读磁盘快照）
python worldbox_agent.py serve            # 默认 127.0.0.1:8777

# 2) 自检
python worldbox_agent.py doctor

# 3) 给每个参战国初始化人格档案
python skills\worldbox-nation-leader\scripts\wb.py persona --me 大仁 --init

# 4) 启动你的房间程序
python your_room.py
```

平台启动时应该做一次**握手自检**，把结果显眼地显示出来：

```python
try:
    st = conn.status()
    print(f"✔ 世界服务就绪（{st.backend}）: {st.world_name} 第 {st.year} 年, "
          f"{st.live_kingdoms} 个存活国家, 数据 {st.save_age_seconds:.0f} 秒前")
    if st.warning:
        print(st.warning)          # 过期警告要显示给用户
except ConnectorError as exc:
    print(f"✘ 世界服务不可用: {exc}")
    print("  请先运行: python worldbox_agent.py serve")
```

### 5.2 每个 agent 的 system prompt 该怎么写

平台负责把三样东西拼进每个 agent 的 system prompt：

```
[1. 你是谁]          ← 平台自己的房间设定
你在 WorldBox 世界「Skulls of Misery」中扮演「大仁」王国的国王。

[2. 你的性格]        ← 来自 data/agents/大仁/persona.md
（谨慎、务实、厌恶无谓的战争；父亲死于叛乱；说话爱用数字…）

[3. 你怎么工作]      ← 告诉它用哪两个 skill
- 用 worldbox-nation-leader 查情报（或直接读平台每轮提供的世界上下文）
- 用 worldbox-leader-policy 的格式输出《国策》
- 决策必须引用真实数据；数据里没有的就说没有，绝不编造
- 你只能提出政策，实际执行者是创世者（玩家）
```

**注意**：如果平台每轮已经用 `agent_context()` 注入了世界状态，
就**不必再让 agent 自己跑 `wb.py`**——那是重复劳动、浪费 token，
而且可能读到与平台不同的时点。两条路选一条：

| 方案 | 适合 | 取舍 |
|---|---|---|
| **平台注入**（推荐） | agent 数量多、要控成本 | 平台统一压缩，所有 agent 看到同一帧；但细节受限 |
| **agent 自查** | agent 少、需要深挖 | agent 可以按需读 `self.json` 全文；但每轮多几 KB 开销 |

可以**混用**：平台注入精简版（4 KB），system prompt 里告诉 agent
"需要更细的城市/军队数据时可以自己去读 `data/nations/大仁/self.json`"。

### 5.3 房间频道设计建议

一条世界频道里混着三种消息，建议视觉上分开：

| 消息类型 | 来源 | 建议呈现 |
|---|---|---|
| **世界事件** | `conn.world_overview()` 的 `全局事件记录` | 系统消息（灰色），如"212 年，大仁攻占琼州" |
| **领袖发言** | 各 agent 的《国策》 | 带动像/国名/性格标签 |
| **外交文书** | 国策「三、对外动作」的子节 | 单独的"信件"卡片，标注发件/收件 |
| **待执行清单** | `journal.export_execution_list()` | 固定侧栏，玩家照着做 |

**外交的关键设计**：`worldbox-leader-policy` 规定和谈/宣战/结盟写成
**外交文书**（发件、收件、事由、现状、请求、让步、威胁、期限）。
你的平台要做的是**把它从一个 agent 投递到另一个 agent 的收件箱**，
并在对方下一轮上下文中提醒"你收到一封信"。

不需要你自己设计协议——skill 已经定义好了文书格式，平台只负责路由。

### 5.4 token 成本与性能

实测（3 个国家的世界）：

| 注入内容 | 体积 |
|---|---|
| `agent_context()` 一个 agent | **约 4 KB** |
| 列国索引 | 1.4 KB |
| 世界概览 | 13 KB |
| 一国完整国情 | 27 KB |

按 5 分钟一轮、10 个国家算：每轮约 40 KB 上下文，一天 288 轮 ≈ 11 MB。
**完全可控**，但前提是**别把 27 KB 的完整国情直接灌进去**。

`WorldConnector` 有 3 秒响应缓存，房间里 N 个 agent 拉同一份数据不会打 N 次请求。

### 5.5 幂等与去重（容易踩的坑）

- **同一指纹只决策一次。** 用 `has_advanced()` 守着。
  否则你的循环快一点，agent 就会在同一帧上决策几十次。
- **同一提案不要重复入账。** 如果 agent 重发上一轮的内容，
  按 `(kingdom_id, summary, fingerprint)` 去重，别让待执行清单里出现 10 条一样的。
- **提案必须带指纹。** `journal.propose(..., fingerprint=status.fingerprint)`，
  这样世界推进后能自动把旧提案标为 obsolete。

### 5.6 错误处理

| 情况 | 应该怎么做 |
|---|---|
| `ConnectorError`（服务没开、无快照） | 显示醒目提示 + 启动指引；**不要崩**；继续让 agent 纯对话 |
| 世界数据过期（`status.stale`） | 横幅警告，并且**把警告注入每个 agent 的上下文**（`agent_context` 已自带） |
| agent 政策解析不到动作 | 保留原文给玩家看，不要丢；提示 agent 按格式重写 |
| 游戏被关闭 | 指纹不再变化，`wait_for_advance` 会超时。回落到"暂停决策、允许对话" |

### 5.7 关于"实时"

必须清楚：**外置工具做不到逐帧实时**，因为游戏本身就只在存档时把世界写到磁盘。

| 想要 | 怎么做 |
|---|---|
| 更快 | **玩家在游戏里手动多存几次档**（下一次轮询 10 秒内读到） |
| 更省 | 把平台的等待设为更长，或 `worldbox_agent.py serve --interval 60` |
| 真·实时 | 超出"非 mod"范围——需要在游戏内挂一个桥接插件，每帧写状态到同一目录。**读取端完全不用改**（接口就是磁盘上那份 JSON）。这需要玩家明确同意。 |

---

## 六、反模式（别这么做）

1. **❌ 每 N 秒无条件开一轮决策。**
   世界没变，agent 只能在同一帧上重复决策。→ 用世界指纹驱动。

2. **❌ 把 27 KB 完整国情灌给每个 agent。**
   成本高、噪声大，agent 反而抓不住重点。→ 用 `agent_context()`。

3. **❌ 只给当前快照，不给趋势。**
   agent 会得出"人口 1767，世界第一强权，随便打"这种结论。
   **趋势才是决策的依据**（示例里同一份数据，谨慎的领袖看到"只恢复到峰值 59%"
   而选择休养；好战的领袖看到"敌人只有 25 兵"而选择出击）。
   → 一定要注入 `history` 序列。

4. **❌ 忘了回填执行结果。**
   agent 提了政策却永远不知道有没有被执行，几轮后就会敷衍。→ 用 `ActionJournal`。

5. **❌ 强迫所有政策都带坐标。**
   减税、停战、不征兵都是真实国策，硬编坐标会让政策变假。
   → 支持"开关型"。

6. **❌ 平台自己替 agent 编数据。**
   如果某个数字读不到，就如实说读不到。**agent 的决策质量完全取决于情报真实性**，
   平台编一个"看起来合理"的数字，等于毁掉整个模拟。

7. **❌ 隐藏数据过期。**
   把 `存档距今秒数` 和警告如实注入，让 agent 自己决定怎么对待。
   隐藏它 = 让 agent 在不知情的情况下把旧战报当实时。

---

## 七、验证你的接入

工具自带回归测试，装完建议先跑：

```powershell
python tools\test_pipeline.py     # 解析/导出/视图 66 项
python tools\test_api.py 8777     # HTTP 接口 17 项（需服务已启动）
python integration\world_connector.py --once   # 适配器自检
```

适配器自检会打印后端、世界指纹、国家清单、压缩后的 agent 上下文（含字节数），
以及一次政策解析演示。**如果这几项都对，你的接入就没问题了。**

---

## 八、需要帮助时

| 问题 | 看哪里 |
|---|---|
| 某个字段是什么意思 | `skills/worldbox-nation-leader/references/数据字典.md` |
| 数据从哪来、怎么解析的 | `docs/外置读取原理.md` |
| 国策格式、政策怎么落地 | `skills/worldbox-leader-policy/SKILL.md` |
| 政策范例（含两个不同人格的对照） | `skills/worldbox-leader-policy/references/国策示例.md` |
| 人格档案怎么填 | `skills/worldbox-leader-policy/references/领袖档案模板.md` |
| 工具整体说明 | `README.md` |

有拿不准的地方，先跑 `python worldbox_agent.py doctor`——
它会告诉你数据能不能找到、解析得对不对、历史库是否可用。
