# 交接文档 · WorldBox 多 Agent 世界频道（第 2 版）

> 写给接手这个项目的下一个 agent。
> 读完本文你应该能：知道已完成什么、为什么这么做、哪些坑不能踩、**接下来必须由人拍板的是什么**。
>
> **本版**由完成「前端存档选择器 + 领袖人设区 + 修复『工具锁住游戏存档』」的 agent 写下。
> 上一版（完成「多存档选择 + 自动生成领袖人设」）的决策与坑已并入本文并保留原文要点。

**最后更新**：本轮会话结束时（2026-09-23 复核目录后定稿）。

---

## ⚠️ 目录现状（2026-09-23 复核，**先读这段**）

项目在本轮会话之后被**重组并纳入 git**。下一位 agent 请先确认你面对的目录，否则会照着过时的路径找文件：

| 东西 | 现在在哪 | 备注 |
|---|---|---|
| **项目 A 数据工具**（本文主体） | `C:\Users\24771\Desktop\world-box-catching-plugin\` | git 仓库（1 个提交 `a0c1ebe "111"`，工作区干净）。`src/ tools/ docs/ skills/ _patch/ data/ worldbox_agent.py` 都在，**本轮的存档锁修复与全部文档均已随之迁移**（已逐项复核）|
| 项目 B 房间程序 | `C:\Users\24771\Desktop\space\` **和** `C:\Users\24771\Desktop\worldbox-agent-bridge\space\` | 两份 `public\app.js` **完全一致**（52809 字节，含存档选择器与人设区），任选其一。判据：`app.js` 里能搜到 `loadWbSaves` 与 `wbPromptBlock` |
| 新的整合仓库 | `C:\Users\24771\Desktop\worldbox-agent-bridge\` | git 仓库，内含 `space/` 与 `world-box-catching-plugin/` 各一份；根目录 README 是作者自述 |
| 旧路径 `world-box-catching-pl` | **已空** | 只剩本文与新增的 skill，项目本体不在这里 |

**所以**：本文中所有相对路径（`src/...`、`tools/...`、`docs/...`）都相对于
`C:\Users\24771\Desktop\world-box-catching-plugin\`。**项目 A 现在是 git 仓库**——
上一版说"本仓库不是 git 仓库"已经过时；改东西前先 `git status` 看基线，备份仍是好习惯。

**本轮提供的两个交付物**：本文（`process.md`）与 `skills/live-system-debugging/SKILL.md`。
两者都放了两处，内容一致：

- 整合仓库根：`C:\Users\24771\Desktop\worldbox-agent-bridge\`（`process.md` + 新建的 `skills\`）
- 项目 A 根：`worldbox-agent-bridge\world-box-catching-plugin\`（skill 与另外三个并列）

**改动请同步两边**，或者只保留整合仓库根那一份（那样请把本文改成指向它的说明）。

---

## 0. 三十秒理解这个项目

两个独立项目，一读一写：

```
WorldBox 游戏
   │ 每约 5 分钟自动存档（autosaves/<时间戳>/map.wbax = 纯 JSON，
   │ map_stats.s3db = SQLite 110 张表）；手动存档 saves/saveN/map.wbox = zlib 压的同一份
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
| A 数据工具 | `C:\Users\24771\Desktop\world-box-catching-pl` | 8777（只绑 127.0.0.1）|
| B 房间程序 | `C:\Users\24771\Desktop\space` | 8787（绑 0.0.0.0，局域网可见）|

---

## 1. 最要紧的六件事（先看这个）

### 🔴 1.1 战争「敌方」恒空，而且同阵营的共同交战国被判成了敌人

20 国世界里 **24/24 条战争记录的 `敌方` 都是空数组**，`正在进行的战争` 的 `防守方` 也全空；
`views.py:404` 的注释写"按双方是否在同一场战争的两侧判断"，但代码只判断**是否在同一场战争里**，
于是「敌人」列表恰好等于「共同交战国」列表——**塔罗共和国、Lyhora 明明和中华帝国同属进攻方，却被标成"交战/敌对"**。

自动人设里因此出现病句：`你目前正在打 3 场战争：对；对；对。`
**在真实演练里，这足以让某个领袖提议攻打自己的盟友。**

修法：关系码改为"同场战争中是否位于对侧"，对侧为空时如实标注"对手已灭亡/未记录"
（项目里已有 `views.py:686` 的"已灭亡"写法可循）；`composePrompt` 在 `敌方` 为空时不要输出"对"。
详见 `docs/多国演练发现-1国到20国.md` P1。

### 🔴 1.2 注入的 8 个邻国按声望选，不按距离

`live_kingdoms()` 按 `(renown, population)` 降序（`build.py:347-350`），桥再 `slice(0, 8)`
（`worldbox-bridge.js:289`）→ **中华帝国的 prompt 里带着 33.1 格外的 Greeshi 的每座城坐标，
却看不到 5.8 格外的塔罗共和国**，而同一段规则第 5 条正是"先查距离再决定调兵"。

修法：`列国` 取"距离最近的 N 个 ∪ 声望最高的 M 个"，并注明"这不是全部邻国"。详见 P2。

### 🔴 1.3 `space/skills/` 仍缺 `worldbox-leader-policy`

房间注入的规则明确要求"用 `worldbox-leader-policy` 技能的格式输出《国策》"，
但 `space/skills/` 和 `room/worldbox*/skills/` 里**只有 `worldbox-nation-leader`**（本会话已复核）。
角色读不到规范，只能靠注入的那几行提示猜格式。

处理：把 `world-box-catching-pl/skills/worldbox-leader-policy/` 复制过去，
并在 `copyWorldboxSkills()` 里加一条"复制完检查两个 skill 都在"的校验。

### 🟠 1.4 注入体量 13.8 KB，代码注释承诺 3–5 KB

20 国下 `systemSection()` 实测 **13.8 KB**（`worldbox-bridge.js:347` 承诺 3–5 KB，
旧文档记 5.4 KB）。其中 `列国` 占 4.8 KB，而 **2.8 KB 是对手城市明细（46 条，无上限）**——
`我的国家` 限 8 座城，`列国` 却把每个对手的城市全列出来。20 角色一轮固定注入约 276 KB。详见 P3。

### 🟠 1.5 「世界变了才重算人设」永远不会触发

`leaderAgent()`（`server.js:151-158`）建房间时给每位领袖写了**非空 `systemPrompt`**，
于是 `autofillSystemPrompts` 里"使用者写过就跳过"的分支（`worldbox-bridge.js:500-505`）
**永远排在 `stale`（世界已变→重算）分支之前**，后者是死代码。
实测 `POST /api/worldbox/prompts {force:false}` → `skipped 20/20`（reason 全是"已有人设，未被覆盖"）。

后果：世界推进后人设不刷新，人设里那句"你的国家正在衰落/恢复（近年人口变化 +N）"
会与实时注入的世界数据互相矛盾。详见 P11。

### ⚠️ 1.6 环境纪律（本轮踩过大坑）

- **8777 / 8787 是上一个 agent 会话内的后台任务**，会话结束可能被回收。接管时先确认：
  ```powershell
  netstat -ano | Select-String ":8777.*LISTENING|:8787.*LISTENING"
  ```
- **游戏可能在运行**（本轮接管时正在玩 `saves\save3`）。**动游戏数据目录前先看 4.0 节**——
  上一个 agent 就是在这里把用户的游戏搞到存不了档的。
- 数据新鲜度：不超过 600 秒才算新鲜，超了所有输出里都会带 `_警告`。**任何世界局势结论都要先看时效。**

---

## 2. 已完成的事项

### 2.1 本轮新增 ✅

| 事项 | 产出 |
|---|---|
| **修复：工具锁住游戏存档**（最严重，见 4.0） | `build.py` 改为读临时副本；`paths.py` 选择状态以最新者为准；`server.py` 暴露 `stats_copied/stats_note`；`tools/test_pipeline.py` 第 7 节回归测试 |
| **前端：存档选择器**（旧文档 5.1 第 1 项） | `space/public/{app.js,index.html,style.css}`：按世界分组、⚠️ 标过期、✅ 标当前生效、409 提示、切完重画面板 |
| **前端：领袖人设区**（旧文档 5.1 第 2 项） | 同上：角色卡上可折叠显示"自动生成 / 既有 systemPrompt / 最终生效"+ 生成时对应的存档与过期标记；世界栏 + 房间设置两个入口；两个按钮（全部 / 只补空缺） |
| **数据侧多国（20 国）全链路验证** | `export` 20 国/103 文件/824ms；`test_pipeline` 72 项、`test_saves` 39 项、`test_api` 19 项全绿；`world_connector --once` 退出码 0；`wb.py whoami/brief` 正常 |
| **记录了 11 个问题 + 1 个未解之谜的答案** | `docs/多国演练发现-1国到20国.md`（P1–P11 + 时间刻度锚点）|
| **新 skill** | `skills/live-system-debugging/SKILL.md`（在活着的系统上排障与改动的纪律，含本轮踩的全部坑）|

**本轮改动的文件清单**

```
src/worldbox_agent/build.py      StatsDatabase 读副本（_copy_db_to_temp）
src/worldbox_agent/paths.py      load_selection 以 updated_at 最新者为准
src/worldbox_agent/server.py     /health 增加 stats_copied / stats_note
tools/test_pipeline.py           第 7 节：库开着时游戏那份必须仍可替换
docs/外置读取原理.md              新增 2.3 节：只读也会锁住游戏
docs/多国演练发现-1国到20国.md     本轮全部发现
skills/live-system-debugging/    新 skill
space/public/{app.js,index.html,style.css}   前端两项功能（在工作区之外）
```

### 2.2 上一版已有 ✅（仍然成立）

| 能力 | 入口 |
|---|---|
| 自检 / 列国 / 导出 / 盯盘 / 本地接口 | `doctor` / `list` / `export` / `watch` / `serve` |
| 打印某国情报 | `show "国名"`（`--others` 看列国简报，`--world` 看世界概览）|
| 列出存档 / 选定存档 | `saves` / `select "#7"`、`select latest` |

**HTTP 接口**（`serve` 后）：`/health`、`/api/v1/nations`、`/api/v1/nations/<id>`、
`/api/v1/nations/<id>/{others,history,events,brief}`、`/api/v1/world`、`/api/v1/events`、
`/api/v1/saves`、`/api/v1/saves/select?key=`、`/api/v1/refresh`。

**两个 skill**：`worldbox-nation-leader`（情报，含 `scripts/wb.py`）、
`worldbox-leader-policy`（政策与人格，定义《国策》固定六节格式与
`执行：请创世者在 <城市名>(x,y) 做什么` 的落点型写法——**房间靠这两条正则抽动作，格式变了抽取就失效**）。

**项目 B 房间程序**：`world_state` 工具、世界局势注入 `buildMessages()`、动作账本 + 限额 + 回填、
`kind:'worldbox'` 房间、`copyWorldboxSkills()`、世界指纹跟踪 + 旧提案作废、`strictTick` 节拍守卫、
`GET/POST /api/worldbox*` 面板接口、多存档选择、自动生成人设。

**测试**（本轮复核后的实际数字）：

| 套件 | 项数 | 命令 |
|---|---|---|
| `tools/test_pipeline.py` | **72**（含新增 6 项） | `python tools\test_pipeline.py` |
| `tools/test_saves.py` | 39 | `python tools\test_saves.py` |
| `tools/test_api.py` | 19（需服务） | `python tools\test_api.py 8777` |
| `test/worldbox-bridge.test.js` | 10 组 | `node test\worldbox-bridge.test.js` |
| `test/worldbox-integration.test.js` | 62 | `node test\worldbox-integration.test.js` |
| `test/worldbox-api.test.js` | 33（需房间程序） | `node test\worldbox-api.test.js 8787` |

> 注意项数会随**世界内容**变化（有些断言在 `for kingdom in ...` 里），这不是 bug——
> 本项目的纪律就是"验证机制，不验证具体数字"。

### 2.3 文档索引（迷路时看这里）

| 想了解 | 看 |
|---|---|
| 总说明、安装、命令 | `README.md`（项目 A）|
| 数据从哪来、怎么解析、有哪些坑 | `docs/外置读取原理.md`（**尤其 2.3 只读也会锁住游戏**）|
| 本轮的 11 个发现与时间刻度锚点 | `docs/多国演练发现-1国到20国.md` |
| 通用对接（Python 适配器版） | `docs/worldbox-房间程序对接文档.md` |
| 给写 agent system prompt 的人 | `docs/worldbox-agent编写速查.md` |
| 房间接入实现 + 多存档 + 自动人设 | `space/docs/世界房间接入指南.md` |
| 怎么给 agent 读游戏数据（方法论） | `skills/game-data-agent-bridge/SKILL.md` |
| 在活着的系统上排障与改动的纪律 | `skills/live-system-debugging/SKILL.md`（本轮新增）|
| 字段含义 | `skills/worldbox-nation-leader/references/数据字典.md` |

---

## 3. 重要的决策（为什么这么做）

### 3.17 读别人的活文件：**读副本，不读原件**（本轮新增，血泪教训）

游戏存档的第一步是 `File.Delete(<存档目录>\map_stats.s3db)`，而 **SQLite 在 Windows 上
打开文件时不共享"删除"权限**。只要工具还开着那个文件，游戏这次存档必然失败。
所以 `StatsDatabase.open()` 先把 `.s3db` 复制到临时文件再打开，**手上一个游戏文件句柄都不留**。

- 代价：每次读多复制一个文件（3 MB 级，毫秒量级）；换来"绝不会让游戏存不了档"。
- 复制可能撞上游戏正在写 → 重试 3 次，并用"能否解析出表清单"验证这次复制完整。
- 临时目录不可用时才退回直接打开，并设置 `note`，通过 `/health#snapshot.stats_note` 暴露——
  **绝不静默降级**。
- 完整证据与复现判据：`docs/外置读取原理.md` 2.3。

### 3.18 选择状态有两个位置时，**以 `updated_at` 最新者为准**（本轮新增）

`worldbox_selection.json` 会在**游戏数据目录**（主）和**工具输出目录**（回退）各出现一次。
旧实现"哪个文件先找到就用哪个"，于是**一份旧选择会盖住新选择**，症状是
"我明明选了它，服务却在读另一个世界"——本轮正是它把工具指到了用户正在玩的存档上。

`null` 是有效状态（=跟随最新），也参与比较，所以更新的"取消锁定"能正确生效。

### 3.19 前端改动只在 `space/public/`，不动服务端（本轮新增）

旧文档 4.4/4.5 反复提醒"四处白名单序列化会吞字段"。本轮**先核对再动手**，结论是
`publicAgent()` 早已暴露 `systemPromptAuto / systemPromptAutoFor / effectivePrompt`，
`worldboxInfo()` 早已暴露 `saveKey / savesCount / line / status / nations`，
`/api/worldbox/{saves,save,prompts}` 三个接口也齐备——**服务端一行都不用改**。
下次加字段前请沿用这个顺序：先查白名单，再决定改哪边。

### 3.20 前端必须兼容**两种字段拼写**（本轮新增）

`GET /api/worldbox/saves` 返回的是 **Python 原始行**（`world_name / age_text / size_mb / has_stats`），
而 `/api/state` 里的 `worldbox.saves` 是**映射过的 camelCase**（`world / age / sizeMb / hasStats`）。
只认一种，换个来源整列就空白。前端用 `wbSaveField(s, 'world', 'world_name')` 之类的兜底读法。

### 3.21 界面措辞必须与数据来源一致（本轮新增）

我第一版把 `systemPrompt` 一律写成"**你手写的**人设"——错的：`leaderAgent()` 建房间时
就已经给每位领袖写了一段默认 `systemPrompt`（实测 `promptManual = 20/20`）。
现在统一叫"**既有 systemPrompt**（不会被自动覆盖）"，并在面板里写明
"只补空缺与过期"在这种房间里通常不会做事。

> 教训：**文案也是一种断言**，会误导使用者。写之前先确认数据是怎么来的。

### 3.22 补丁方式：锚点断言 + 备份 + 写后自检 + 一次授权（本轮新增）

要改的文件在工作区之外（`space\`），沙箱默认拒绝。做法是写一个补丁脚本：
每个锚点必须**恰好命中一次**否则整体中止；自动适配 CRLF/LF 与 BOM；写前备份到
`space\.backup-savepick\` / `space\.backup-prompts\`；写后重读自检并 `node --check`。
先用 `--dry-run` 验证锚点，再实跑一次（被拒后按规则申请一次更宽权限）。
**好处：只弹一次授权，而且失败时不会留下半吊子状态。**

### 3.1–3.16（上一版决策，仍然有效，要点保留）

1. **读文件，不读内存**：不注入进程、不逆向；代价是延迟 0–5 分钟。**但读文件也要守 3.17。**
2. **`map_stats.s3db` 是最有价值的数据源**：JSON 只有"当前一帧"，历史表才让 agent 判断兴衰。
3. **键名英文、值中文**：程序可安全依赖键；人有可直接读值；枚举另给 `label`。
4. **一国一文件夹**：agent 只开自己那份；`slugify()` 要保留中文并处理 Windows 保留名。
5. **人口口径必须处处一致**：`civ_kingdom_id` 匹配的角色数、**排除船只**；索引/self/others/world 同一口径。
6. **字段口径是逐个核对出来的**：军队=带 `army` 字段的单位数（城市记录里没有）；领土=`zones` 长度和；
   建筑按 `buildings[].cityID` 反查；城市坐标=`zones` 重心。**校验手段是和游戏自己的 `KingdomYearly1` 对账。**
7. **军队即时值取单位上的字段**（`army_size_map[army_id]`）。
8. **过期要代码主动报警**（`STALE_AFTER_SECONDS=600`，写 `_警告`、往 stderr 打提示），不靠文档纪律。
9. **由"世界推进"驱动，不由定时器驱动**：指纹 = `存档路径|时间戳`；想快就让玩家多手动存档。
10. **平台级上下文必须压缩**：保留最要紧的少量明细 + **完整趋势**；需要细节走工具按需取。
11. **同一指标两套时间点都给出**（即时值 + `history` 值），并在 `口径歧义提示` 里说明。
12. **存档选择必须显式**：永远取"最新"会把房间接到使用者不关心的世界上；
    `latest` 是**指令**，必须在解析存档名之前拦截。
13. **人设自动生成，但绝不覆盖手写**：存独立字段 `systemPromptAuto`，记录 `systemPromptAutoFor` 指纹；
    最终人设 = 手写 + 自动。
14. **切换存档要连带处理三件事**：清空动作账本、按新世界重建领袖、重算所有人设；房间运行中拒绝切换（409）。
15. **测试要验证机制，不要依赖环境**：断言写 `isinstance(x, list)`，国家名一律动态取。
16. **静默失败最危险**：区分"没数据"和"报错"，查询失败要记进 `db.error`。

---

## 4. 踩过的坑

### 4.0 ⚠️ 只读也会锁住游戏（本轮踩的最大的坑）

**症状**：用户报"工具读取存档后，下一次存档无法保存"。游戏 `Player.log`：

```
Error during saving
System.IO.IOException: The process cannot access the file
'...\WorldBox\saves\save3\map_stats.s3db' because it is being used by another process.
  at System.IO.FileSystem.DeleteFile ...      ← 失败点是 Delete
  at db.DBManager.saveToPath → SaveManager.saveStatsIn → ... → clickSaveSlot
```

**根因**：工具（`serve`）长期持有 `saves\save3\map_stats.s3db` 的 SQLite 连接
（`refresh()` 只在**替换快照**时关旧连接，`server.py:213-214`）；而游戏存档要先
`File.Delete` 那个文件 → 必然失败。**而 `save3` 正是用户正在玩的那份手动存档**——
"把工具指向我现在这个世界"是最自然的用法，于是必然中招（自动存档不断新建目录，症状反而隐蔽）。

**定位过程（值得复用的方法）**：
1. 读对方的日志拿到异常堆栈（**失败点是 `File.Delete`** 是决定性线索）；
2. 看文件时间戳分布：同目录 `map.meta`/`preview.png` 更新到最新，**只有 `.s3db` 停住**；
3. `/health` 显示 `using = save3` → 确认工具读的正是那一份（**别用"我以为它在读 X"下结论**）；
4. 非破坏性探针：**申请 DELETE 权限但不真删**（`CreateFileW` + `0x00010000`，share=7），
   错误 32 = 有人不允许删除 → 这就是"游戏能否存档"的判据。打开前 ✅ / 连接期间 ❌ / 修复后 ✅。

**修复**：见 3.17。**验收**：工具运行并读着 save3 时，`saves\save3\map_stats.s3db` 仍 ✅ 可删除，
且 `/history` 查询正常（返回 17 行）。回归测试在 `tools/test_pipeline.py` 第 7 节。

> **移交提醒**：修复只对**重启后**的工具生效。如果谁还开着旧代码的 `serve`/`watch`，它仍会锁住存档。

**事后验证（2026-09-23 复核游戏目录）**：修复后游戏**确实恢复存档**——
`saves\save3\` 的 5 个文件（含 `map_stats.s3db`）时间戳统一为 **2026-09-20 19:45:26**，
根目录 `stats.s3db` 同一时刻更新（证明"删除旧库→从根库复制"这条路走通了）。
`Player.log` 里共 8 次 `Error during saving`，最后一次仍指向同一个库，顺序在成功存档之前。
**这类"我修好了没有"必须回到现场验证，而不是只看单元测试。**

### 4.1–4.10（上一版，仍然有效）

1. **SQLite 跨线程**：`check_same_thread=False` + 一把锁；否则工作线程**静默返回空**。
2. **`PRAGMA table_info` + `Row`**：`r[0]` 是列序号，`r[1]` 才是列名；错了 SQL 变 `SELECT  FROM`，**不报错只返回空**。
3. **只读打开避免抢锁**：`file:...?mode=ro&immutable=1`——**但它正是 4.0 的源头**，现在改用副本。
4. **原子写入**：临时文件 + `os.replace`，否则读者可能看到半个 JSON。
5. **白名单序列化会吞掉新字段**：症状是"我明明写进去了，读出来没有"——第一反应查白名单，别怀疑写入。
6. **关键词指令要在解析之前拦截**（`latest`/`auto`/`none`/`*`），否则会被当成实体名锁死选择。
7. **HTTP handler 里别用 `res` 当变量名**（遮蔽响应对象 → `res.writeHead is not a function`）。
8. **给异常日志加堆栈**，别只打 `e.message`。
9. **测试不要依赖具体数据**（世界从 3 国变 1 国时一批断言集体失效）。
10. **临时目录的限制**：`tempfile.mkdtemp()` 建的目录在某些沙箱下不能再建子目录，测试用工作区内目录并清理。

### 4.11 用"独占打开"判断占用是**无效实验**（本轮）

我最初用 `FileShare.None` 打开存档文件来判断"是不是被占用"，结果**所有文件都报被占用**——
因为游戏自己也开着它们。这个实验无法归因，只会把责任推错对象。
**判据必须针对"对方真正要做的那个操作"**（本例是删除 → 申请 DELETE 权限）。

### 4.12 我的测量错误四连（本轮，全部差点被当成 bug 报出去）

| 我看到的"bug" | 真相 |
|---|---|
| 每国历史**恰好 8 行** | 我读了 `h.get("history", h)`，真键名是 `series` → 量到顶层键个数；真值 19 行 |
| 我的城市数 **= 0** | 键名写错（真实键名是 `城市列表_按无家可归排序_仅前8座`），代码本来是对的 |
| 注入体量合计 **0.0 KB** | 辅助函数只接受字符串，我把**数字**传进去 |
| `world_connector` 退出码 **-1** | `Select-Object -First 40` 截断管道所致；真值 0 |

外加 `Get-Content -Raw` 把 UTF-8 中文显示成乱码（文件本身正常）。
**整齐得可疑的数字、醒目的非零退出码，先怀疑测量方式。** 详见新 skill 第三节。

### 4.13 `job_kill` 不会杀掉子进程（本轮）

`job_kill` 取消的是 pwsh 包装进程，**python/node 子进程会存活并继续占着端口/文件**。
本轮就出现过：杀掉 job 后 8777 仍在监听。收尾要用 `Stop-Process -Id <PID> -Force`
（`taskkill` 在同一场景下报 Access denied，而 `Stop-Process` 成功）。

---

## 5. 待完成的事项，以及**必须由人拍板的决策**

### 5.1 建议的动手顺序（不需要拍板，但要先修 1.1/1.2 再演练）

1. **修 P1（战争关系）与 P2（列国按距离）**——否则多国演练只会得出"agent 看不到对手、
   看不见近邻"的假结论，并把 bug 误记到 agent 的决策能力上。
2. 补 `space/skills/` 缺的 `worldbox-leader-policy`（1.3）。
3. 修 P3（注入体量）与 P11（默认 systemPrompt 阻断重算）。
4. 起 8777 + 8787，做一次**多国世界完整演练**（旧文档 5.3，至今未做）。

### 5.2 需要人拍板的决策

| # | 决策 | 选项 | 我的建议 |
|---|---|---|---|
| D1 | **房间接到哪个世界** | (a) `save3`（你现在玩的手动存档：Skulls of Misery 第 206 年，**2 国**）(b) `#7` Isles of Despair（第 118 年，**20 国**，38 天旧） | 想验证"多国博弈"这条主线就选 (b)；想跟当前游戏联动就选 (a)。**两者不要混用**——切存档会重建领袖并作废旧提案 |
| D2 | 是否现在修 P1/P2 | 修 / 先演练再修 | 先修。P1 会让领袖打盟友 |
| D3 | P11 怎么修 | (a) 默认文案改写入 `systemPromptAuto`　(b) 加 `systemPromptIsDefault: true` 标记 | (b) 改动最小且语义清楚 |
| D4 | **写回通道**（超出现有边界） | (a) 维持"agent 只提案、玩家手工执行"　(b) 在游戏内挂 BepInEx 插件，让决策真的作用于游戏 | 这是旧文档 5.5A 的老问题，**必须先征得用户同意**；读取端完全不用改 |
| D5 | 时间刻度锚点是否采信 | 见 5.3 | 值得用一次实测确认后再写进 skill 与数据字典 |

### 5.3 附带收获：两套时间刻度的锚点（旧文档 5.5B 的答案）

旧文档说 `world.年份`（历史纪年）与事件/战争的年份（世界时间）**无法换算**，并猜"存档里可能有锚点"。
本轮找到了——**锚点不在数据库里，是一条换算比例**：

- 证据：10 个带统计库的存档、跨 6 个世界，`MAX(WorldLogMessage.timestamp) / MAX(WorldYearly1.timestamp)`
  **全部聚在 ~60**（7079/118 = 59.99；60.62、60.98 这两个略大于 60 可用"年度表最大年份可能比当前年份小 1"解释，
  上限 `60 + 60/最大年份`）。
- 结论（**经验规律，未经游戏代码确认**）：**世界时间 ≈ 历史年份 × 60**。
- 意义：事件/战争年份除以 60 即可与 `history` 对齐 → "**战争先发生还是饥荒先发生**"这类跨刻度排序**可以回答**。
- **可证伪实验**：在游戏里推进 1 年，看 `WorldLogMessage` 最大 `timestamp` 是否正好 +60。

### 5.4 其它未做完的（旧文档遗留 + 本轮新增）

- **P4** `/health` 的 `save_selection.mode` 在服务启动时读到的持久化锁定会**谎报"跟随最新存档"**
  （`server.py:145-150` 的 `mode` 由进程内 `_requested_selector` 决定）。前端已**故意不显示** `mode`。
- **P5** `list`/`show`/`doctor` 无视持久化选择（`common(p, out=False)` 让它们读不到回退位置的状态文件）：
  实测选中 #7 时 `show 中华帝国` 直接失败（退出码 2）。
- **P6** `关系矩阵` 恒空 `{}`，而 `others.json` 里的 `关系矩阵说明` 被桥丢弃 → agent 分不清"没数据"和"没解释"。
- **P7** `data/agents/` 下只有已灭亡的「大仁」（孤儿人设），现存国家都没有 `persona.md`；演练前要批量 init。
- **P8** 趋势窗口只有约 19 年（**游戏自身滚动保留**，不是工具截断：`WorldYearly1` 在第 78 年的世界里也是 19 行）。
  建议在 `history_meta`/README 写明，否则 agent 会以为"国家只有 19 年历史"。
- **P9** 客户端正常断开会让服务端打印整段 `ConnectionResetError` 堆栈（正常现象，看着像崩溃）。
- **P10** `/api/state` 的 `savesCount` 报成 1（实际 31）：`worldboxInfo(withSaves:false)` 用 `bridge.saves(1)` 取数，
  而 Python 的「存档数量」是 `len(rows)`。
- **Python 适配器没有存档选择方法**（旧文档 5.4）：`integration/world_connector.py` 缺 `saves()`/`select_save()`，
  Node 版 `worldbox-bridge.js` 有。
- 旧文档 5.6 的小事：`/api/v1/nations/<id>` 有 45 KB（20 国时），前端按需取；
  `room/worldbox{,2,3}` 三个测试房间可清理；`start.bat` 未确认是否设置了 `PYTHONIOENCODING`。

---

## 6. 我犯过的错（合并两版，供参考）

1. **过早基于"世界是 3 国 212 年"写死断言与示例**（上一版）→ 世界一变，一批测试和文档同时失效。
2. **以为 `select latest` 生效了**，其实被当成存档名锁死（上一版）→ 保留词必须在解析前拦截。
3. **被 PowerShell 的 stderr 包装骗过**（上一版 + 本轮）：`wb.py` 的过期警告写到 stderr，
   PowerShell 显示成 `NativeCommandError`，我一度以为命令失败 → **看显式退出码，别信红字**。
4. **`sendJSON(res, ...)` 里 `res` 被局部变量遮蔽**（上一版）。
5. **示例里教了一个荒谬的军事部署**（上一版）："把大仁 XV（62 人）交给大辽"——实测大辽距莱国 **26 格**，
   是全军离敌人最远的部队 → **文档里的每个数字都要真的算过；错误的示例比没有示例更糟**。
6. **本轮：把测量错误当成 bug**（4.12 的四连）。
7. **本轮：把服务端默认人设叫成"你手写的"**（3.21）——文案也是断言。
8. **本轮最严重：我的 8777 进程锁住了用户正在玩的 save3，导致游戏无法存档**（4.0）。
   而且我一开始还用**无效实验**（独占打开）给自己的组件"洗清"了嫌疑；最后是靠
   `/health` 显示的 `using=save3` + DELETE 探针才定位到。
   → **教训：先怀疑自己的组件，再怀疑别人；判据要针对对方真正的操作。**

---

## 7. 环境与操作备忘

### 7.1 沙箱与授权

- 本会话沙箱是 **workspace-write**：只能写 `world-box-catching-pl`。
  **写 `space\` 需要 `danger-full-access`**（本会话已多次被拒→批准）。改前端前先想好要写哪几个文件，
  合并成一次操作，别十次弹窗。
- 被拒后按规则用**同一条命令**申请一次更宽权限并说明理由；不要绕过。

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

### 7.3 接管时第一件事

```powershell
# 1) 两个服务在不在
netstat -ano | Select-String ":8777.*LISTENING|:8787.*LISTENING"
# 2) 游戏在不在跑（在跑就要格外小心，见 4.0）
Get-Process worldbox -ErrorAction SilentlyContinue
# 3) 工具当前读的是哪一份（别用旧读数）
python -c "import urllib.request,json; h=json.load(urllib.request.urlopen('http://127.0.0.1:8777/health')); s=h['snapshot']; print(s['save_key'], s['world_name'], s['year'], s['kingdoms'], s['stats_copied'])"
# 4) 数据侧回归
python worldbox_agent.py doctor
python tools\test_pipeline.py           # 72
python tools\test_saves.py              # 39
python tools\test_api.py 8777           # 19（需服务）
python integration\world_connector.py --once
# 5) 项目 B
cd C:\Users\24771\Desktop\space
node test\worldbox-bridge.test.js
node test\worldbox-integration.test.js
node test\worldbox-api.test.js 8787     # 33（需房间程序）
```

### 7.4 我留下的东西

> **复核（2026-09-23）**：8777 / 8787 **都没在跑**（我起的后台任务已随会话结束被回收，
> 与本节原来预期的"可能被回收"一致）；游戏也在 2026-09-20 19:49 之后没有再写日志。
> 下一位要做的第一件事就是按 7.2 起服务、按 7.3 做体检。

| | |
|---|---|
| 数据服务 | 需要时按 7.2 启动：`python worldbox_agent.py serve`（8777）|
| 房间程序 | 需要时按 7.2 启动：`node server.js`（8787）|
| 房间状态 | 上次为「房间 2」`room.saveKey = save3`；配置在 `space\data\` 与 `space\room\` 下 |
| 工具选择 | `data\worldbox_selection.json` = `save3`（游戏目录那份是 `null` 且更旧，按新规则不生效）|
| 前端备份 | `space\.backup-savepick\`（补丁前原文件，**回滚用这份**）、`space\.backup-prompts\`（人设区各阶段）|
| 补丁脚本 | `world-box-catching-plugin\_patch\*.js`（锚点断言 + 备份逻辑，可复现可回滚；确认无误后可删）|

**回滚前端**：

```powershell
Copy-Item 'C:\Users\24771\Desktop\space\.backup-savepick\app.js.1789897629317.bak'     'C:\Users\24771\Desktop\space\public\app.js' -Force
Copy-Item 'C:\Users\24771\Desktop\space\.backup-savepick\index.html.1789897629317.bak' 'C:\Users\24771\Desktop\space\public\index.html' -Force
Copy-Item 'C:\Users\24771\Desktop\space\.backup-savepick\style.css.1789897629317.bak'  'C:\Users\24771\Desktop\space\public\style.css' -Force
```

### 7.5 游戏数据目录里的文件（接管时别踩）

```
%USERPROFILE%\AppData\LocalLow\mkarpenko\WorldBox\
├── autosaves\<时间戳>\{map.wbax, map.meta, map_stats.s3db, preview.png}
├── saves\saveN\{map.wbox, map.meta, map_stats.s3db, preview.png}
├── stats.s3db / stats.json       ← 游戏自己的活库，**不要碰**
├── worldbox_selection.json       ← 工具写在这里（不可写时退到工具 data\）
├── Player.log / logs\error_*.log ← **排查游戏问题先看这里**
```

### 7.6 PowerShell 的假失败信号（本项目高发）

- `... | Select-Object -First N` 会截断管道，让 `$LASTEXITCODE` 变非零；
- 程序写 **stderr** → 被包装成红色 `NativeCommandError`，退出码其实是 0；
- `Get-Content -Raw` 显示中文乱码，文件本身可能是好的（编辑前先确认编码）。

---

## 8. 一句话交接

**地基是稳的**：数据管道、三个 skill、房间接入、220+ 项测试、字段口径都是逐项核对过的。

**本轮最大的两件事**：① 把"工具锁住游戏存档"这个会**弄坏用户游戏**的 bug 修掉了（读副本），
并留下了回归测试与复现判据；② 旧文档 5.1 的前端两项（**存档选择器、领袖人设区**）已实现并实测，
第三项（过期横幅）经核实本来就正常（`wb.line` 里的 ⚠️ 用 `textContent` 渲染，不会截断）。

**最要紧的三件事**：修 P1（战争关系，会让领袖打盟友）与 P2（列国按距离）、
补 `space/skills/` 缺的 skill、然后在**一个多国世界**里真跑一遍完整闭环。

**最容易踩的两个坑**：① **只读也会锁住游戏**——动游戏目录前先读 4.0 与
`docs/外置读取原理.md` 2.3；② 这个项目有四处**白名单式序列化**
（`publicAgent`/`publicRoom`/`publicRoomFull`/`worldboxInfo`），加字段漏一处，症状就是"写进去了但读不出来"。

**最该记住的纪律**：先怀疑自己的测量，再怀疑代码；先怀疑自己的组件，再怀疑别人；
报告要分清事实、推断与未验证；错了立刻说。
