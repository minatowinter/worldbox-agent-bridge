# 交接文档 · WorldBox 多 Agent 世界频道

> 写给接手这个项目的下一个 agent。
> 读完本文你应该能：知道已完成什么、为什么这么做、哪些坑不能踩、接下来做什么。

**最后更新**：由上一个 agent 在完成「多存档选择 + 自动生成领袖人设」后写下。

---

## 0. 三十秒理解这个项目

有两个独立项目，一读一写：

```
WorldBox 游戏
   │ 每约 5 分钟自动存档（map.wbax = 纯 JSON，map_stats.s3db = SQLite）
   ▼
【项目 A】world-box-catching-pl   ← 数据工具（Python，零依赖）
   │ 解析存档 → 按国家拆成情报文档 → 开本地 HTTP 接口
   │ 产出：data/nations/<国名>/{self,others,history,events}.json
   ▼
【项目 B】space（Agent Room）      ← 多 agent 对话平台（Node，零依赖）
   │ 把世界局势注入角色 system prompt → 角色产出《国策》
   │ → 收集成"待执行清单" → 创世者（玩家）在游戏里照做 → 结果回填
   ▼
创世者（人类玩家）
```

**关键约束**：agent **不能直接改游戏**，只能"提案"；真正动手的是玩家。
这是整个架构的地基，任何设计都不能违背它。

| 项目 | 路径 | 端口 |
|---|---|---|
| A 数据工具 | `C:\Users\24771\Desktop\world-box-catching-pl` | 8777 |
| B 房间程序 | `C:\Users\24771\Desktop\space` | 8787 |

---

## 1. 最要紧的三件事（先看这个）

### ⚠️ 1.1 房间工作区里缺一个 skill

`space/skills/` 和 `room/worldbox*/skills/` 里**只有 `worldbox-nation-leader`**，
**缺 `worldbox-leader-policy`**。

但注入给角色的规则明确要求"用 `worldbox-leader-policy` 技能的格式输出《国策》"
（六节格式、`A. 落点型` / `B. 开关型` 的写法）。**角色读不到那个规范**，
只能靠注入的那几行提示猜格式。

**处理**：把 `world-box-catching-pl/skills/worldbox-leader-policy/`
复制到 `space/skills/`，并重跑一次 skill 复制（或手工复制到各
`room/<dir>/skills/`）。然后考虑在 `copyWorldboxSkills()` 里加一条校验：
复制完检查两个 skill 都在，缺了就记日志。

### ⚠️ 1.2 当前世界只剩一个国家

现在选中的存档是「Skulls of Misery 第 194 年」，**只有「大仁」1 个国家**
（原来的莱国、蜀立都不存在了）。这意味着：

- "多国博弈"这条主线**目前无法真正验证**；
- 三套测试里凡是假设"必须有他国"的断言都已被改成动态取值。

**建议**：换一个国家多的存档（`#7 Isles of Despair` 有 20 国）做一次完整演练，
或让玩家在游戏里重新分裂出几个国家。

### ⚠️ 1.3 数据可能过期

存档距今超过 600 秒就算过期。当前经常是 20 分钟以上（游戏没开）。
**任何"世界局势"的结论都要先看新鲜度**，这条纪律对人和 agent 同样适用。

---

## 2. 已完成事项

### 2.1 项目 A：数据工具 ✅

| 能力 | 入口 |
|---|---|
| 自检 | `python worldbox_agent.py doctor` |
| 列出国家 | `python worldbox_agent.py list` |
| 导出快照 | `python worldbox_agent.py export` |
| 持续盯盘 | `python worldbox_agent.py watch` |
| 本地接口 | `python worldbox_agent.py serve` |
| 打印某国情报 | `python worldbox_agent.py show "大仁"` |
| **列出存档** | `python worldbox_agent.py saves` |
| **选定存档** | `python worldbox_agent.py select "#7"` |

**数据来源**（已实测确认）：

| 文件 | 内容 | 格式 |
|---|---|---|
| `autosaves/<时间戳>/map.wbax` | 完整世界状态，约每 5 分钟 | **纯 JSON**（`saveVersion 17`）|
| `autosaves/<时间戳>/map_stats.s3db` | 逐年统计 + 事件流水 | SQLite（110 张表）|
| `saves/saveN/map.wbox` | 手动存档 | zlib 压缩的同一份 JSON |

**产出结构**：

```
data/
├── _index.json            列国索引：国名 → 编号 → 目录
├── world_overview.json    世界概览、国力排行、坐标范围
├── world.json             完整原始快照
├── worldbox_selection.json 存档选择（或退到游戏数据目录）
├── agents/<国名>/         领袖人格档案（persona.md / memory.md）—— 不会被导出覆盖
└── nations/<国名>/{self,others,history,events}.json + README.txt
```

**HTTP 接口**（`serve` 后）：

| 接口 | 说明 |
|---|---|
| `GET /health` | 存活、**世界指纹**、新鲜度、存档选择 |
| `GET /api/v1/nations` | 列国索引 |
| `GET /api/v1/nations/<编号或国名>` | 一国完整国情（含城市坐标）|
| `GET /api/v1/nations/<id>/others` | 列国简报（含**敌城坐标**、领土主张线索）|
| `GET /api/v1/nations/<id>/history` | 逐年趋势 |
| `GET /api/v1/nations/<id>/events` | 本国大事记 |
| `GET /api/v1/world` | 世界概览 |
| `GET /api/v1/events` | 全球事件 |
| `GET /api/v1/saves` | **列出可选存档** |
| `GET /api/v1/saves/select?key=...` | **切换存档** |
| `GET /api/v1/refresh` | 强制重读 |

### 2.2 两个 skill ✅

| Skill | 作用 |
|---|---|
| `worldbox-nation-leader` | 情报：怎么查自己国家和别国现状（含 `scripts/wb.py` 查询工具）|
| `worldbox-leader-policy` | 政策与人格：怎么把局势变成可执行操作，并保持领袖形象一致 |

`worldbox-leader-policy` 定义了固定六节格式（零/一/二/三/四/五），
其中 **`A. 落点型` 必须写成 `执行：请创世者在 <城市名>(x,y) 做什么，数量多少`**，
**`B. 开关型` 写成 `执行（全国开关）：把 <政策> 调整到 <档位>`**——
房间程序靠这两条正则抽取可执行动作，**格式变了抽取就会失效**。

### 2.3 项目 B：房间程序接入 ✅

`server.js` 里已实现：

- `world_state` 工具（只读，只在 WorldBox 房间出现）
- 世界局势注入 `buildMessages()`（约 5.4 KB，含**趋势**）
- `pollDecision` 只注入人设、不注入战报（省 token）
- 动作账本 `room.actions` + `worldboxBudget()` 限额 + `worldboxFeedback()` 回填
- `kind: 'worldbox'` 房间类型，建房间时按世界国家自动建领袖
- `copyWorldboxSkills()` 复制 skill 进工作区
- `syncWorld()` 世界指纹跟踪 + `expireStaleActions()` 旧提案作废
- `strictTick` 节拍守卫（世界没推进就不开新一轮）
- `GET/POST /api/worldbox*` 世界面板接口
- **多存档选择**：`GET /api/worldbox/saves`、`POST /api/worldbox/save`
- **自动生成领袖人设**：`syncWorldboxPrompts()` + `POST /api/worldbox/prompts`

`worldbox-bridge.js`（新增，零依赖）提供：`WorldBridge`（状态/国家/上下文/存档）、
`systemSection()`、`autofillSystemPrompts()`、`composePrompt()`、`rolePromptFor()`。

### 2.4 测试 ✅ 共 220 项全绿

| 套件 | 项数 | 命令 |
|---|---|---|
| `tools/test_pipeline.py` | 68 | 解析/视图/导出/人口口径/坐标/过期警告 |
| `tools/test_saves.py` | 39 | 存档发现/选择/持久化/回退 |
| `tools/test_api.py` | 18 | HTTP 接口（需服务在跑）|
| `test/worldbox-bridge.test.js` | 10 组 | 桥接自检（状态/节拍/解析/降级/缓存）|
| `test/worldbox-integration.test.js` | 62 | 桥接逻辑 + 决策闭环 + **存档选择 + 自动人设** |
| `test/worldbox-api.test.js` | 33 | **实机 API**（需房间程序在跑）|

### 2.5 文档 ✅

| 文档 | 位置 | 用途 |
|---|---|---|
| `README.md` | 项目 A | 总说明 |
| `docs/外置读取原理.md` | 项目 A | 数据从哪来、怎么解析、有哪些坑 |
| `docs/worldbox-房间程序对接文档.md` | 项目 A | 通用对接（Python 适配器版）|
| `docs/worldbox-agent编写速查.md` | 项目 A | 给写 agent system prompt 的人 |
| `docs/世界房间接入指南.md` | 项目 B | **接入实现说明 + 多存档 + 自动人设** |

---

## 3. 重要决策（为什么这么做）

这些决策是踩过坑才定下来的，**改之前请先读理由**。

### 3.1 读文件，不读内存

不注入进程、不读内存、不逆向对象布局。只读游戏**自己写到磁盘的文件**。

- **理由**：不碰进程 → 不崩游戏、不需管理员权限、不算作弊；
  存档格式比内存布局稳定得多，游戏小版本更新不会让工具失效。
- **代价**：延迟 0–5 分钟（游戏存档节奏，外置工具改不了）。

### 3.2 `map_stats.s3db` 是最有价值的数据源

JSON 快照只有"当前一帧"，而 SQLite 里有 `KingdomYearly1`（每国逐年 48 个指标）
和 `WorldLogMessage`（事件流水）。

- **理由**：**没有趋势，agent 只能描述现状，无法判断兴衰**。
  实测中同一份数据，看到"人口只恢复到峰值 59%"的谨慎国王选择休养，
  看到"敌人只有 25 兵"的好战国王选择出击——这就是趋势的价值。
- **实现**：`history` 字段必须出现在每份注入的上下文里。

### 3.3 键名英文、值中文

JSON 的**键**是固定英文（`identity`、`ruler`、`status`、`demographics`…），
**值**是简体中文（`"名称"`、`"国王"`、`"人口"`）。标签另给 `label` 字段
（如 `{"id": "wise", "label": "睿智"}`）。

- **理由**：键名稳定 → 程序可以安全依赖；值中文 → agent 与人直接可读。
- **代价**：文档里必须写清每个键的含义（见 `references/数据字典.md`）。

### 3.4 一国一文件夹

`data/nations/<国名>/` 而不是一个大 JSON。

- **理由**：领袖 agent 只需要打开自己的目录，不必在 24 MB 里筛选；
  也不会误读到别国的内部细节。
- **注意**：`slugify()` 要保留中文、处理 Windows 保留名（CON/PRN…）。

### 3.5 人口口径必须处处一致（曾经错过）

**人口 = `actors_data` 里 `civ_kingdom_id` 匹配的角色数，排除船只。**

- **踩过的坑**：`views.py` 排除船只、`build.py::population_of` 不排除，
  导致同一国家在索引/本国/国力排行三处出现**三个不同数字**（1767 / 1784）。
  agent 看到会以为数据有错。
- **解决**：`kingdom_units` 只存平民，船只另存 `kingdom_boats`；
  `_index`、`self`、`others`、`world` 全部用同一口径。
- **有测试守着**：`test_pipeline.py` 的「逐城人口之和 == 全国人口」。

### 3.6 字段口径是逐个核对出来的，不是猜的

| 指标 | 正确算法 | 踩过的坑 |
|---|---|---|
| 人口 | `civ_kingdom_id` 匹配的角色数，**排除船只** | 见 3.5 |
| 军队 | 上述角色中**带 `army` 字段**的数量 | 一开始读 `cities[].army`——**城市记录根本没这个字段**，恒为 0 |
| 领土 | 各城市 `zones` 数组长度之和 | `zones` 一格一条 |
| 建筑 | `buildings` 里 `cityID` 匹配的条目数 | 城市记录也没有 `buildings` |
| 城市坐标 | 该城 `zones` 所有格子的**重心** | 城市不存坐标，只能自己算 |

**校验手段**：和游戏自己的 `KingdomYearly1` 交叉对比。
本工具算出军队 309、逐年统计 356——差异来自"统计年初记一次、即时值是存档时刻"，
**两者都对**。这是确认没读错字段的唯一办法。

### 3.7 军队即时值要用单位上的字段

`army` 字段在**单位**身上（值是军队 id），不是在城市/王国记录上。
`army_size_map[army_id]` 与 `kingdom_armies` 派生出 `army_size_of()`。

### 3.8 数据过期主动报警，不靠文档纪律

文档里写"超过 600 秒要提醒用户"是不够的——**要代码主动报警**。

- 超过 `STALE_AFTER_SECONDS = 600` → 文档里出现 `_警告` 字段；
- `wb.py` 在 **stderr** 打印醒目提示（不污染 stdout 的 JSON）；
- `statusLine()` 显示 ⚠️ 横幅。

### 3.9 由"世界推进"驱动，不由定时器驱动

指纹 = `存档路径|存档时间戳`（`status().fingerprint`）。

- **理由**：游戏约每 5 分钟才存档一次，指纹不变就是世界没变。
  世界没变还开新一轮 → 角色在同一帧上反复产出同一份《国策》，
  会迅速毁掉对话质量。
- **实现**：`strictTick` 守卫 + `worldbox.lastFingerprint`。
- **想让世界快**：玩家在游戏里手动多存几次档。**不是调循环频率。**

### 3.10 平台级上下文必须压缩

完整国情 27 KB → `agent_context()` 压到约 4 KB。

- **保留**：八座最要紧的城市（按**无家可归**排序，那是叛乱前兆且是真实分城数据）、
  **完整趋势序列**、列国对比、坐标。
- **丢弃**：全部城市明细、军队番号明细（需要时用 `world_state` 工具按需取）。
- **理由**：agent 的 context 是最稀缺资源；27 KB 灌进去反而抓不住重点。

### 3.11 同一个指标有两套时间点，都给出

| 指标 | 即时值 | 历史值 |
|---|---|---|
| 人口 | `demographics.人口` | `history[].population` |
| 军队 | `military.军队总兵力` | `history[].army` |
| 声望 | `status.声望` | `history[].renown` |

**不要选一个假装另一个不存在**——在 `knowledge.口径歧义提示` 里同时列出，
并说明"判断当前国力用即时值，判断趋势用 history"。

### 3.12 存档选择必须显式

本机实测 **31 个存档**，横跨 1411 天、多个世界。

- **理由**：永远取"最新"会把房间接到一个使用者根本不关心的世界上，
  或者一个几小时前的旧世界而 agent 不自知。
- **接口**：`保存选择` 支持文件夹名 / `#序号` / 世界名 / `latest`；
  状态文件写在游戏数据目录，不可写时**退到工具 data/ 并如实告知**（不静默失败）。
- **关键**：`latest` 是"恢复跟随最新"这条**指令**，不是存档名——
  必须在解析前拦截，否则会被当成存档名把选择锁死。

### 3.13 人设自动生成，但绝不覆盖手写

四条约束（`autofillSystemPrompts()`）：

| 约束 | 理由 |
|---|---|
| 绝不覆盖 `systemPrompt` | 那是使用者对角色的设定，优先级最高 |
| 存进独立的 `systemPromptAuto` | 一眼看出哪段是系统补的；清空即重新生成 |
| 只在空白或世界已变时生成 | 否则每轮重写，把上下文搅乱 |
| 记录 `systemPromptAutoFor` 指纹 | 换存档/世界推进后必须重算，旧国名可能不存在了 |

最终人设 = `手写 + 自动`（`rolePromptFor()` 拼接）。

### 3.14 切换存档要连带处理三件事

换了世界，很多东西就不成立了：

1. **清空动作账本** —— 旧提案针对的局势已不存在；
2. **按新世界重建领袖** —— 国名、国王、国家数都可能完全不同；
3. **重算所有人设**。

**房间正在运行时拒绝切换（409）**，否则会出现"一半角色还在旧世界"的错乱。

### 3.15 测试要验证机制，不要依赖环境

这是被现实教出来的：世界从"3 国 212 年"变成"1 国 194 年"之后，
一堆断言集体失效：

- 「世界事件非空」→ 新开局的世界一条事件都没有；
- 「可指定查询他国」→ 世界只剩一个国家；
- 「硬编码国名 大仁/莱国」→ 国家会改名、会灭亡。

**原则**：断言写成 `isinstance(x, list)` / "字段存在" / "机制可用"，
而不是"必须有 N 条"。国家名一律动态取。

### 3.16 静默失败是最危险的，必须区分"没数据"和"报错"

踩过两个**不报错、只是返回空**的坑（详见第 4 节）。
现在查询失败会记进 `db.error`，而不是无声咽下。

---

## 4. 踩过的坑（照着避开）

### 4.1 SQLite 跨线程

HTTP 主线程建快照、工作线程答查询，`sqlite3` 默认禁止跨线程复用连接，
抛 `SQLite objects created in a thread can only be used in that same thread`。

**表现极具迷惑性**：启动时读事件日志（主线程）正常，
请求历史数据（工作线程）**静默返回空**——因为异常被 `except sqlite3.Error` 吞了。

**解法**：`sqlite3.connect(..., check_same_thread=False)` + 一把锁串行化。

### 4.2 `PRAGMA table_info` 的列名陷阱

设了 `row_factory = sqlite3.Row` 后，`PRAGMA table_info` 返回的行是
`(cid, name, type, ...)`，**`row[0]` 是列序号（0,1,2…）而不是列名**。

```python
cols = [r[0] for r in ...]   # 错：拿到 0,1,2… → 匹配不上任何字段
cols = [r[1] for r in ...]   # 对：索引 1 才是列名
```

结果 SQL 变成 `SELECT  FROM ...`，查询返回空，**同样不报错**。

### 4.3 Node 里用 `res` 当局部变量名

```js
const res = await syncWorldboxPrompts(...);   // 遮蔽了 HTTP 响应对象 res
return sendJSON(res, 200, {...});             // → res.writeHead is not a function → 500
```

**教训**：在 HTTP handler 里，`res` 是保留名。用 `result` / `filled` 之类。

### 4.4 加字段时改错了地方

我给 `publicRoom()` 加了 `saveKey`，但 `/api/state` 用的是 `worldboxInfo()`——
**`saveKey` 在服务端生效却没传到前端**。查了很久。

**教训**：加字段前先搜"这个数据从哪里序列化出去"，
本项目有 `publicAgent` / `publicRoom` / `publicRoomFull` / `worldboxInfo` 四处
**白名单式**构造，任何一处漏加，字段就"消失"。

### 4.5 白名单构造会悄悄吞掉新字段

`publicAgent()` 是白名单，我加的 `systemPromptAuto` 不在里面，
于是**人设写进了磁盘上的 `config.json`（330 字节），API 读回来却是 0**。

**教训**：这类 bug 的症状是"我明明写进去了，读出来没有"——
第一反应应该是查序列化白名单，而不是怀疑写入失败。

### 4.6 `normalizeAgent` 的兜底人设会阻断自动生成

原本 `systemPrompt: a.systemPrompt || '你是一个有自己立场和想法的角色。'`
会给 WorldBox 领袖塞一句兜底人设，于是 `autofillSystemPrompts`
把它当成"使用者手写过"而拒绝生成。

**解法**：兜底改成 `''`，让"空白"真的是空白。

### 4.7 `mkdtemp` 在沙箱下不能建子目录

测试里用 `tempfile.mkdtemp()` 建的目录，**不允许再创建子目录**。
导出需要 `nations/by-id` 这种层级，会 `PermissionError`。

**解法**：测试用工作区内的普通目录（`ROOT/".test-tmp"/"export"`），
并在结束时清理。

### 4.8 `fetch` 的 keep-alive 让进程退出时报 libuv 断言

Node 脚本结束时如果还有 keep-alive 连接，某些版本会
`Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)`。

**解法**：`WorldBridge` 提供 `dispose()` 清缓存；测试结束调它，
并用 `process.exitCode = ...` 而不是 `process.exit()`。

### 4.9 PowerShell 的假失败信号

- `something | Select-Object -First N` 会截断管道，让 `$LASTEXITCODE` 变非零；
- 命令把内容写到 **stderr** 时，PowerShell 会包装成 `NativeCommandError`，
  即使退出码是 0。

**解法**：判断成功与否看**显式打印的退出码**，不要被 `[exit code: 1]` 带偏。
（这一条我自己也被骗过一次，见第 6 节。）

### 4.10 GBK 编码

`space/docs/世界房间接入指南.md` 曾经是 GBK 编码，PowerShell 显示乱码，
但浏览器正常。**编辑前先确认编码**，否则大段替换会失败
（`old_string` 匹配不上）。

---

## 5. 待完成事项

### 5.1 🔴 前端：存档选择器（用户明确要求）

**现状**：`public/app.js`、`public/index.html` **完全没有**接新 API。

实测：

```
app.js 含 '/api/worldbox/saves'   : False
app.js 含 '/api/worldbox/save'    : False
app.js 含 '/api/worldbox/prompts' : False
app.js 含 'systemPromptAuto'      : False
app.js 含 'effectivePrompt'       : False
```

**已有**（别人做的，可复用风格）：世界状态横幅、`r_worldboxBox` 面板、
`strictTick` 开关、按角色指派国家（`nationOf`）、动作审批、`autoAssign`。

**要做**：

1. **存档下拉框**
   - 数据源 `GET /api/worldbox/saves` → `{ dataDir, count, selection, locked, saves[] }`
   - 每项字段：`key, index, kind, world, time, age, stale, hasStats, sizeMb, selected`
   - **按 `world` 分组**（同一个世界有很多存档，不分组会很乱）
   - 默认项"跟随最新存档"（对应 `key: "latest"`）
   - 选中后 `POST /api/worldbox/save { key }`
   - 显示 `age` 与 `stale`（过期的要标红——这是选存档时最重要的信息）
   - 409 时提示"房间正在运行，请先停止"
   - 成功后刷新整个面板（领袖会重建）

2. **人设区**
   - 角色卡上显示 `systemPromptAuto`（可折叠）与 `effectivePrompt`
   - 加"重新生成人设"按钮 → `POST /api/worldbox/prompts { force: true }`
   - 明确标注"手写的人设不会被覆盖"
   - 显示 `systemPromptAutoFor` 对应的世界（判断是否过期）

3. **数据过期横幅**（`line` 字段已含 ⚠️，确认渲染时不截断）

> ⚠️ **注意**：`/api/state` 的 `worldbox` **不再携带完整存档列表**
> （只带 `savesCount`），这是刻意的性能设计——
> 完整列表必须走 `GET /api/worldbox/saves`。别在 `/api/state` 里找 `saves`。

### 5.2 🟠 补上缺失的 skill

见第 1.1 节。把 `worldbox-leader-policy` 复制进 `space/skills/`
与各 `room/<dir>/skills/`，并在 `copyWorldboxSkills()` 里加校验。

### 5.3 🟠 在多国世界做一次完整演练

当前世界只有 1 国，**"多国博弈"这条主线还没真正验证过**。
建议用 `#7 Isles of Despair`（20 国）或让玩家重新分裂出几个国家，
跑一次完整的：多角色发言 → 各自产出《国策》→ 外交文书 → 动作清单 →
玩家执行 → 回填 → 下一轮。

### 5.4 🟡 两个适配器不一致

`integration/world_connector.py`（Python 版，通用平台用）**没有**存档选择方法，
而 `worldbox-bridge.js`（Node 版）有。

- 若要给非 Agent Room 的平台用，给 Python 版补上 `saves()` / `select_save()`。
- 或者明确在文档里写"Python 版不含存档选择"。

### 5.5 🟡 两个未解决的设计问题

**A. 写回通道（超出"非 mod"边界，需用户同意）**

现在 agent 只能提案，玩家手工执行。若要让决策真的作用于游戏，唯一干净路径是
在游戏内挂一个 BepInEx 插件（游戏已装 BepInEx），每帧把状态写到同一目录。
**读取端完全不用改**（接口就是磁盘上那份 JSON）。
但这已越过"非 mod"，**必须先征得用户同意**。

**B. 两套时间刻度无法换算**

`world.年份`（如 194）是"历史纪年"，`history.timestamp` 与事件 `年份`（如 12468）
是"世界时间"，起点不同、**无法换算**。

后果：agent **答不出"是战争先发生还是饥荒先发生"**这类跨刻度排序问题。
目前只能让 agent 如实说"无法对齐"。

**可能的解法**（未验证）：存档里可能同时存在两套刻度的锚点
（如 `mapStats.world_time` 与 `history_current_year`），
若能找到对应关系就能建立统一时间轴。**值得查一下**——
这对"讲清一个国家的兴衰史"很关键。

### 5.6 🟡 小改进

- `content-length` / 大响应：`/api/v1/nations/<id>` 有 27 KB，前端按需取
- `room/worldbox`、`worldbox2`、`worldbox3` 三个测试房间可以清理
- `start.bat` 未确认是否设置了 `PYTHONIOENCODING`（数据服务需要）

---

## 6. 我犯过的错（供参考，别重复）

1. **过早基于"世界是 3 国 212 年"写死了断言与示例**。
   世界变成 1 国 194 年后，一大批测试和文档示例同时失效。
   → **教训**：测试和示例都不要依赖具体世界的具体数字。

2. **一度以为 `select latest` 生效了**，实际它被当成存档名把选择锁死了。
   → **教训**：关键词指令（`latest`/`auto`）必须在解析存档名之前拦截。

3. **被 PowerShell 的 stderr 包装骗过**：`wb.py` 的过期警告写到 stderr，
   PowerShell 把它显示成 `NativeCommandError`，我一度以为命令失败了。
   → **教训**：看显式退出码，别信红字。

4. **`sendJSON(res, ...)` 里 `res` 被局部变量遮蔽**，端点 500，
   而日志只打了 `e.message` 没有堆栈，查了几轮。
   → **教训**：先给异常日志加堆栈，再猜原因。（我后来加了，
   立刻定位到 `server.js:1756`。）

5. **示例文档里教了一个荒谬的军事部署**：写"把大仁 XV（62 人）交给大辽慢慢磨莱国"，
   实测大辽距莱国最近 **26 格**，是全军离敌人最远的部队。
   → **教训**：文档里的每个数字都要真的算过；示例错了比没有示例更糟。

---

## 7. 环境与操作备忘

### 7.1 沙箱注意事项

- 我的沙箱只能写 `world-box-catching-pl`。写 `space/` 需要用户授权
  （`sandbox_permissions`）。**下一位如果也受限，记得申请。**
- **关键**：`space/data/` 目录有沙箱 ACL，**房间程序在那里写 `config.json`**。
  如果以受限沙箱启动 `node server.js`，会
  `EPERM: operation not permitted, open '...\data\config.json'` 而启动失败。
- `%USERPROFILE%\AppData\LocalLow\` 在我的沙箱里只读，
  所以 `worldbox_selection.json` 会退到工具 `data/` 目录——
  **真实运行时它应该写在游戏数据目录**。

### 7.2 启动方式

```powershell
# 终端 1：数据服务（必须）
cd C:\Users\24771\Desktop\world-box-catching-pl
$env:PYTHONIOENCODING='utf-8'
python worldbox_agent.py serve          # 8777

# 终端 2：房间程序
cd C:\Users\24771\Desktop\space
node server.js                          # 8787
```

### 7.3 我留下的后台进程

我停止过用户的房间程序（PID 5176，加载的是旧代码），并重启到 8787。
截止本文写作时，8777 与 8787 都在运行，但**都是我启动的会话内后台任务**，
我的会话结束后可能被清理。**接手时先确认两个服务在不在**：

```powershell
netstat -ano | Select-String ":8777.*LISTENING"
netstat -ano | Select-String ":8787.*LISTENING"
```

### 7.4 验证命令（接手第一件事就跑）

```powershell
# 项目 A
cd C:\Users\24771\Desktop\world-box-catching-pl
python worldbox_agent.py doctor
python tools\test_pipeline.py          # 68
python tools\test_saves.py             # 39
python tools\test_api.py 8777          # 18（需服务）
python integration\world_connector.py --once

# 项目 B
cd C:\Users\24771\Desktop\space
node test\worldbox-bridge.test.js          # 10 组
node test\worldbox-integration.test.js     # 62
node test\worldbox-api.test.js 8787        # 33（需房间程序）
```

---

## 8. 一句话交接

**地基是稳的**：数据管道、两个 skill、房间接入、220 项测试都是好的，
口径是逐个核对过的（不是猜的）。

**最要紧的三件事**：补上缺失的 `worldbox-leader-policy` skill、
做前端存档选择器、在一个多国世界里真跑一遍。

**最容易踩的坑**：这个项目有四处**白名单式序列化**
（`publicAgent`/`publicRoom`/`publicRoomFull`/`worldboxInfo`），
加字段漏一处，症状就是"写进去了但读不出来"。
