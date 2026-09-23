# worldbox-agent

**WorldBox 0.51.2 国家情报外置工具** —— 让本地 AI agent 扮演国家领袖时，能读到真实的世界数据。

这不是 mod。它不注入游戏进程、不读写内存、不改动存档。它只做一件事：
读取 WorldBox **自己写到磁盘上的文件**，把整个世界拆成「每个国家一份」的情报文档，
并开一个本地接口供 agent 查询。

## 它解决什么问题

你想让每个 agent 扮演一个国家，根据真实局势做决策、展现领袖性格。难点在于
agent 看不到游戏。本工具补上这一环：

```
WorldBox  ──写存档──>  worldbox_agent  ──分国家情报──>  agent（国家领袖）
                        （本工具）          + 本地HTTP接口
```

## 快速开始

```powershell
# 1. 自检：能不能找到并解析游戏数据
python worldbox_agent.py doctor

# 2. 启动（会自动导出一次，然后持续盯盘 + 开接口）
python worldbox_agent.py serve

# 3. 看结果
python worldbox_agent.py list
```

打开 <http://127.0.0.1:8777/api/v1/nations> 就能看到所有国家。

只想要文件、不要服务，用：

```powershell
python worldbox_agent.py export     # 导出一次到 ./data
python worldbox_agent.py watch      # 持续保持最新
```

## 运行要求

- **Python 3.10+**，**不需要任何第三方库**（只用标准库，不用 pip install）
- WorldBox 0.51.2（PC 版），已至少自动存档过一次
- Windows（本机路径自动识别；其他平台可用 `--data-dir` 指定）

## 数据从哪来

| 来源 | 内容 |
|---|---|
| `autosaves/<时间戳>/map.wbax` | 完整世界状态，**纯 JSON**（约每 5 分钟一个） |
| `autosaves/<时间戳>/map_stats.s3db` | SQLite 历史库：**逐年统计** + **世界事件日志** |
| `saves/saveN/map.wbox` | 手动存档，内容同上但是 zlib 压缩 |

`map_stats.s3db` 里游戏自己维护了 110 张表，包括
`KingdomYearly1/10/100`（每国逐年的 48 个指标）、`CityYearly*`、`WarYearly*`、
`WorldYearly*`、`WorldLogMessage`（事件流水）。这是本工具最有价值的数据源——
**它给了 agent「记忆」**：不只看当前一帧，还能看出一个国家是在崛起还是崩解。

### 外置读取的原理

完整的技术说明见 [`docs/外置读取原理.md`](docs/外置读取原理.md)。一句话概括：

> 不碰游戏进程，只读游戏**自己写到磁盘上的文件**——
> `map.wbax` 是纯 JSON，`map.wbox` 是 zlib 压缩的同一份 JSON，
> `map_stats.s3db` 是 SQLite。三者都无需注入、无需逆向内存布局，
> 因此对游戏版本的变化也相对不敏感。

关键工程细节（都已在真实存档上验证）：

- **自动识别两种容器**：按首字节判断是 JSON（`{`）还是 zlib 流，后者用 `zlib.decompress` 解压
- **只读打开 SQLite**：`mode=ro&immutable=1`，避免与正在运行的游戏抢文件锁
- **跨线程连接**：HTTP 工作线程要用到主线程创建的连接，必须 `check_same_thread=False` 加锁
- **`PRAGMA table_info` 的坑**：设了 `row_factory = sqlite3.Row` 后，`row[0]` 是**列序号**而不是列名——
  这个坑会让所有历史查询静默返回空（不报错）
- **原子写入**：先写临时文件再 `os.replace`，agent 并发读取时不会读到半个文件

## 输出长什么样

```powershell
python worldbox_agent.py export
```

```
data/
├── README.txt                  给 agent 的使用说明
├── _index.json                 ★ 列国索引：国名 → 编号 → 目录（先读这个）
├── world_overview.json         世界概览、国力排行、全球战争
├── world.json                  完整原始快照（机器用）
├── MANIFEST.json               本次导出的清单
└── nations/
    ├── 大仁/
    │   ├── README.txt          这一国怎么读
    │   ├── self.json           ★ 本国完整国情（15 KB 左右）
    │   ├── others.json         ★ 他国横向对比 + 战争/立场矩阵
    │   ├── history.json        逐年时间序列
    │   └── events.json         本国大事记
    ├── 莱国/ ...
    └── by-id/
        └── 6.json              按编号直接取，不必先查名字
```

**一国一文件夹**是核心设计：领袖 agent 只需要打开自己的目录，不必在
24 MB 的大 JSON 里自己筛选，也不必担心读到别国的内部细节。

## HTTP 接口

`python worldbox_agent.py serve` 之后：

| 接口 | 说明 |
|---|---|
| `GET /health` | 存活状态、数据新鲜度、最近错误 |
| `GET /api/v1/world` | 世界概览与国力排行 |
| `GET /api/v1/nations` | 列国索引（国名 → 编号） |
| `GET /api/v1/nations/<编号或国名>` | 某国完整国情 |
| `GET /api/v1/nations/<编号或国名>/others` | 某国视角下的列国简报 |
| `GET /api/v1/nations/<编号或国名>/history?history=KingdomYearly1` | 逐年趋势 |
| `GET /api/v1/nations/<编号或国名>/events` | 某国大事记 |
| `GET /api/v1/events?limit=200` | 全球事件流水 |
| `GET /api/v1/refresh` | 立即重新读取存档 |

`<编号或国名>` 支持 `6`、`#6`、`大仁` 或唯一的一段子串。
参数：`compact=1` 关闭美化缩进，`limit=N` 限制行数。

## 给 agent 用的 skill

`skills/` 下有两个配套 skill，分别解决"看到什么"和"做什么"：

| Skill | 作用 | 关键文件 |
|---|---|---|
| `worldbox-nation-leader` | **情报**：怎么查自己国家和别国的现状 | `SKILL.md`、`references/数据字典.md`、`scripts/wb.py` |
| `worldbox-leader-policy` | **政策与人格**：怎么把局势变成创世者能执行的操作，并保持领袖形象一致 | `SKILL.md`、`references/国策示例.md`、`references/领袖档案模板.md` |

安装方式取决于你的 agent 框架，把两个文件夹都放到技能目录即可：

```powershell
Copy-Item -Recurse skills\worldbox-nation-leader "$env:USERPROFILE\.agents\skills\"
Copy-Item -Recurse skills\worldbox-leader-policy "$env:USERPROFILE\.agents\skills\"
```

`worldbox-leader-policy` 里的 `wb.py` 引用自 `worldbox-nation-leader`，两个一起装最省事。

### 查询小工具

```powershell
$wb = "skills\worldbox-nation-leader\scripts\wb.py"
python $wb whoami                     # 我是谁 / 列出所有国家
python $wb brief --me 大仁            # 极简摘要（最省 token）
python $wb self  --me 大仁            # 完整国情（含城市坐标）
python $wb others --me 大仁           # 列国对比、敌城坐标、领土主张线索
python $wb history --me 大仁 --last 15
python $wb events --me 大仁
python $wb world                      # 世界概览与国力排行
python $wb persona --me 大仁          # 查看领袖人格档案状态
python $wb persona --me 大仁 --init   # 初始化 persona.md / memory.md
```

优先走 HTTP 接口，接口没开时自动回退读 `data/` 里的文件，两种模式都能用。

### 政策是怎么落地的

**agent 只能提议，不能直接改世界**——真正动手的是创世者（玩家）。
所以 `worldbox-leader-policy` 要求每份国策都给出**带地图坐标的可执行清单**，
例如"请创世者在 北平(4,23) 增建房屋，补足 83 人容量"。

为此工具在每座城市上算出了**重心坐标**（`territory.城市列表[].坐标`），
它可以直接当作投放食物、矿物或施放神力的落点：

```json
{ "名称": "北平", "坐标": [4, 23], "人口": 323, "建筑数": 496, "无家可归": 83 }
```

`others.json` 里同样带坐标，还包括**敌城列表**与**领土主张线索**
（对方哪些城市曾属于别国——判断"这仗该不该打"的唯一依据）。
`world_overview.json` 里的 `world.可用坐标范围` 给出地图边界。
（注意 `world.存档引擎尺寸字段` 是游戏内部分块数，**不是**地图大小，
别拿它判断范围——这个字段名就是为防这个坑而设计的。）

### 人口口径

所有文档的「人口」都**不含船只**，所以可以互相直接比较：

| 位置 | 口径 |
|---|---|
| `self.json` → `demographics.人口` | 常住人口，不含船只 |
| `self.json` → `demographics.含船只条目数` | 加回船只的存档条目数 |
| `territory.城市列表[].人口` | 逐城常住人口（同样不含船只） |
| `others.json` / `_index.json` / `world_overview.json` → `人口` | 同上 |

逐城人口之和 == 全国人口（这一点有回归测试守着，因为早期版本这里出过错）。

### 配合 agent 的 system prompt

你只需要在 system prompt 里告诉 agent 它是哪个国家、什么性格：

```
你是 WorldBox 世界里「大仁」王国的国王，性格谨慎而务实，厌恶无谓的战争。
每次决策前：
1. 用 worldbox-nation-leader 技能读取本国与他国的真实情报；
2. 用 worldbox-leader-policy 技能的方法，输出一份带坐标的国策；
3. 在「内心」一节写下你真实的想法，哪怕它和最优解冲突。
```

若要让**多个国家各由一个 agent 扮演**，给每个 agent 不同的国名即可——
每个 agent 只读自己的 `nations/<国名>/` 目录，彼此不知道对方的内部细节，
跨 agent 的外交通过「外交文书」进行（格式见 `worldbox-leader-policy`）。

## 字段口径（重要）

这些口径都对着本机真实存档逐一核对过：

| 指标 | 口径 |
|---|---|
| 人口 | 存档中 `civ_kingdom_id` 归属该国的角色数（精确值） |
| 军队 | 上述角色中带军队标记的数量（即在编军人） |
| 领土 | 该国各城市所占地块数之和 |
| 建筑 | 存档中 `cityID` 属于该国城市的建筑条目数 |

**已知限制**（本工具宁可说"不知道"，也不编数字）：

- **没有角色年龄**——存档不写这个字段，所以不提供"国王几岁"
- **粮食、国库、忠诚度只在 `history` 里**，城市记录本身不保存
- **军队即时值 vs 逐年统计值会有小差异**——统计是每年年初记一次，
  即时值是当前存档时刻。判断趋势用 `history`，判断"现在能打多少"用即时值
- **数据延迟 0–5 分钟**——取决于游戏何时自动存档；每份文档都带
  `world.存档距今秒数`，agent 应当检查它

## 命令一览

```
python worldbox_agent.py doctor                 自检：能否找到并解析游戏数据
python worldbox_agent.py export                 导出一次快照到 ./data
python worldbox_agent.py watch                  持续保持 ./data 最新
python worldbox_agent.py serve                  盯盘 + 本地 HTTP 接口
python worldbox_agent.py list                   列出存档里的所有国家
python worldbox_agent.py show "大仁"            打印某国完整情报
python worldbox_agent.py show "大仁" --others   打印列国简报
python worldbox_agent.py show --world           打印世界概览
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--data-dir <路径>` | 指定 WorldBox 数据目录（默认自动查找） |
| `--out <路径>` | 输出目录（默认 `./data`） |
| `--port <端口>` | HTTP 端口（默认 8777，被占用会自动往后找） |
| `--interval <秒>` | 存档轮询间隔（默认 10） |
| `--history-table` | `KingdomYearly1` / `10` / `100`，历史时间分辨率 |
| `--autosaves-only` | 忽略手动保存的世界 |

## 关于实时性

WorldBox 自动存档间隔约 5 分钟，这是**游戏本身**的节奏，外置工具无法改变它。
所以：

- 想要更快的刷新，**在游戏里手动按一次保存**，下一次轮询（默认 10 秒内）
  就会读到新数据
- 想要更慢/更省资源，把 `--interval` 调大
- agent 每次决策前都应检查数据新鲜度

## 目录结构

```
worldbox_agent.py                 命令行入口
src/worldbox_agent/
├── paths.py                      存档目录发现
├── build.py                      存档解析 + 索引构建（含 SQLite 历史）
├── views.py                      面向 agent 的中文视图
├── export.py                     分国家写盘
└── server.py                     本地 HTTP 接口
integration/
└── world_connector.py            房间程序适配器（见下）
skills/
├── worldbox-nation-leader/       skill 一：情报（看什么）
│   ├── SKILL.md
│   ├── references/数据字典.md     全字段含义与口径
│   └── scripts/wb.py             查询小工具
└── worldbox-leader-policy/       skill 二：政策与人格（做什么）
    ├── SKILL.md
    └── references/
        ├── 国策示例.md            两个不同人格的对照范例 + 反例
        └── 领袖档案模板.md         人格设定模板
docs/
├── 外置读取原理.md               数据从哪来、怎么解析、有哪些坑
├── 世界房间程序对接文档.md        ★ 给多 agent 对话平台开发者（通用）
└── worldbox-agent编写速查.md      给写 agent system prompt 的人
tools/test_pipeline.py            解析/导出/视图回归（66 项）
tools/test_api.py                 HTTP 接口回归（17 项）
tools/test_integration.py         房间程序适配器回归
```

## 接到多 agent 对话平台上

如果你的房间程序要让多个 agent 各扮演一国领袖在世界频道里对话、
做决策，直接用 `integration/world_connector.py`（纯标准库，import 或复制皆可）：

```python
from integration.world_connector import WorldConnector, ActionJournal

conn = WorldConnector("http://127.0.0.1:8777", data_dir="data")
journal = ActionJournal("data/actions.json")

status = conn.status()                       # 世界指纹 + 数据新鲜度
if conn.has_advanced(last):                  # 世界推进了才开新一轮
    for name in 参战国:
        ctx = conn.agent_context(name,       # 压到 ~4 KB 的 agent 上下文
                                 recent_actions=journal.recent_decided(...))
        reply = agent.say(room="world", context=ctx)
        for act in WorldConnector.parse_policy_actions(reply, kid, name):
            journal.propose(kid, name, act["kind"], act["summary"],
                            coord=act["coord"], fingerprint=status.fingerprint)
    room.show(journal.export_execution_list())   # 玩家照着在游戏里执行
```

三个设计要点（详见对接文档）：

1. **由世界推进驱动，不是由定时器驱动**——游戏约每 5 分钟存档一次，
   指纹没变就重复决策会让 agent 在同一帧上反复说同样的话。
2. **注入压缩过的上下文**——完整国情 27 KB，`agent_context()` 压到约 4 KB，
   但保留最关键的**趋势**（没有趋势，agent 只会描述现状而无法判断兴衰）。
3. **必须有动作闭环**——agent 只能提案，玩家执行；执行结果要回填，
   否则 agent 会觉得自己在对着空气说话。

完整说明见 [`docs/世界房间程序对接文档.md`](docs/世界房间程序对接文档.md)。

> **已经接入 Agent Room（`C:\Users\24771\Desktop\space`）**：
> 该项目的 `worldbox-bridge.js` 就是这个适配器的 Node 版，
> 接入步骤见那边的 [`docs/世界房间接入指南.md`](../../space/docs/世界房间接入指南.md)（四处小改动），
> 自检与端到端测试在其 `test/` 目录下。
