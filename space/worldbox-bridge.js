'use strict';
/*
 * worldbox-bridge.js —— Agent Room 的 WorldBox 世界接入桥
 *
 * 零第三方依赖（与 server.js 风格一致，只用 Node 18+ 内置模块）。
 *
 * 它把 worldbox-agent 外置工具读到的世界状态接进房间，提供三样东西：
 *
 *   1. TOOL_DEF           一个 `world_state` 工具定义，直接塞进 server.js 的 TOOLS
 *   2. systemSection()    一段系统提示，把当前世界局势注入每个角色的上下文
 *   3. WorldBridge 类     缓存 + 节拍器（世界推进检测）+ 优雅降级
 *
 * 设计要点（为什么这样写）：
 *
 * · **只在 WorldBox 房间里出现。** 房间配置里加 `worldbox: { enabled: true }`
 *   才暴露工具；普通房间完全不受影响。
 * · **带缓存。** buildMessages() 每回合都会被调用（包括"按需发言"的
 *   YES/NO 判断请求），不能每次都去请求接口。默认缓存 8 秒。
 * · **世界推进才开新一轮。** 游戏约每 5 分钟自动存档一次，指纹不变就是
 *   世界没变。`bridge.hasAdvanced()` 用来守住这个节拍，避免角色在同一帧
 *   数据上反复决策、反复说同样的话。
 * · **优雅降级。** 世界服务没开时不抛异常，而是返回一段明确的提示，
 *   让角色知道"现在没有世界情报"，而不是让它凭空编造局势。
 *
 * 接入方式见 docs/世界房间接入指南.md（4 处小改动）。
 */

const DEFAULT_BASE = 'http://127.0.0.1:8777';
const STALE_AFTER_SECONDS = 600;   // 超过 10 分钟视为数据过期

/* ================================================================== */
/* 工具定义（塞进 server.js 的 TOOLS）                                 */
/* ================================================================== */

const TOOL_DEF = {
  write: false,
  desc:
    '获取 WorldBox 世界局势。参数：{"nation":"国名或编号"}（省略则用你扮演的国家）。' +
    '返回你本国的人口/军队/城市/资源、历年趋势、列国军力对比、战争与外交状态，' +
    '以及数据新鲜度。做任何决策之前都应该先调用它。',
  run: async (room, args) => {
    const bridge = getBridge(room);
    if (!bridge) return '（本房间未接入 WorldBox 世界数据）';
    const nation = (args && args.nation) || resolveNationFor(room, args || {});
    return bridge.stateText(nation);
  },
};

/* ================================================================== */
/* WorldBridge                                                        */
/* ================================================================== */

class WorldBridge {
  /**
   * @param {object} opts
   * @param {string} opts.baseUrl   worldbox_agent 服务地址
   * @param {number} opts.cacheMs   响应缓存毫秒
   * @param {number} opts.timeoutMs 单次请求超时
   */
  constructor(opts = {}) {
    this.baseUrl = String(opts.baseUrl || DEFAULT_BASE).replace(/\/+$/, '');
    this.cacheMs = Number(opts.cacheMs ?? 8000);
    this.timeoutMs = Number(opts.timeoutMs ?? 15000);
    this._cache = new Map();     // path -> { at, data }
    this._lastStatus = null;
    this._lastError = '';
  }

  /* ---------- 原始请求（带缓存） ---------- */

  async _get(pathAndQuery) {
    const now = Date.now();
    const hit = this._cache.get(pathAndQuery);
    if (hit && now - hit.at < this.cacheMs) return hit.data;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const res = await fetch(this.baseUrl + pathAndQuery, { signal: ctrl.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      this._cache.set(pathAndQuery, { at: now, data });
      this._lastError = '';
      return data;
    } catch (e) {
      this._lastError = e.name === 'AbortError' ? '请求超时' : String(e.message || e);
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /* ---------- 世界状态 ---------- */

  /** 当前快照身份与新鲜度。返回 null 表示世界服务不可用。 */
  async status() {
    const health = await this._get('/health');
    const snap = health && health.snapshot;
    if (!snap) return null;
    const ageSec = Number(snap.save_age_seconds || 0);
    const st = {
      fingerprint: `${snap.source}|${snap.save_timestamp}`,
      worldName: snap.world_name,
      year: snap.year,
      kingdoms: snap.kingdoms,
      liveKingdoms: snap.live_kingdoms,
      saveTimestamp: snap.save_timestamp,
      saveAgeSeconds: ageSec,
      statsDb: !!snap.stats_db,
      stale: ageSec > STALE_AFTER_SECONDS,
    };
    this._lastStatus = st;
    return st;
  }

  /** 世界是否推进了（存档指纹变了）。用来把"开新一轮决策"挂在世界节拍上。 */
  async hasAdvanced(prevStatus) {
    const now = await this.status();
    if (!now) return false;
    if (!prevStatus) return true;
    return now.fingerprint !== prevStatus.fingerprint;
  }

  /** 人类可读的一行状态，用于房间 UI 显示。 */
  async statusLine() {
    const st = await this.status();
    if (!st) return `⚠️ 世界服务不可用（${this._lastError || '未启动'}）`;
    const mins = (st.saveAgeSeconds / 60).toFixed(1);
    const stale = st.stale ? `  ⚠️ 数据已 ${mins} 分钟旧，可能已过期` : `  数据 ${mins} 分钟前`;
    return `🌍 ${st.worldName} 第 ${st.year} 年 · 存活 ${st.liveKingdoms}/${st.kingdoms} 国${stale}`;
  }

  /* ---------- 国家 ---------- */

  async nations() {
    const data = await this._get('/api/v1/nations?compact=1');
    return (data && data.kingdoms) || [];
  }

  /* ---------- 存档（多存档时必须能选） ---------- */

  /**
   * 列出数据目录里的所有存档（最新在前）。
   *
   * 为什么需要：一个正常玩家有几十个存档，分属不同世界与时间
   * （本机实测两组存档相差 38 天、世界名都不同）。如果永远只取"最新"，
   * 房间就会被接到一个使用者根本不关心的世界上——或者一个几小时前的旧世界。
   *
   * 列表只读 `map.meta`（几 KB），**不会**解析几十 MB 的存档本体。
   */
  async saves(limit = 0) {
    const q = limit > 0 ? `?limit=${limit}` : '?limit=200';
    const data = await this._get('/api/v1/saves' + q);
    if (!data) return { saves: [], selection: null, count: 0, error: this._lastError };
    return {
      saves: data.saves || [],
      selection: data['当前选择'] || null,
      count: data['存档数量'] || 0,
      dataDir: data['数据目录'] || null,
    };
  }

  /**
   * 切换使用哪一份存档。立刻重新读取，并清空本地缓存。
   * @param {string|null} key  存档 key / #序号 / 世界名 / 'latest'（恢复跟随最新）
   */
  async selectSave(key) {
    const q = encodeURIComponent(key == null ? 'latest' : String(key));
    const res = await this._get(`/api/v1/saves/select?key=${q}`);
    this.clearCache();          // 换了存档，旧数据全部作废
    this._lastStatus = null;
    return res || { ok: false, error: this._lastError };
  }

  clearCache() {
    this._cache.clear();
  }

  /** 把 国名 / 编号 / #编号 / 唯一子串 解析成一个国家条目。 */
  async resolve(token) {
    const nations = await this.nations();
    if (!nations.length) return null;
    const s = String(token ?? '').trim();
    if (/^#?\d+$/.test(s)) {
      const id = Number(s.replace('#', ''));
      return nations.find((n) => n.id === id) || null;
    }
    const low = s.toLowerCase();
    const exact = nations.filter((n) => String(n['名称'] || '').toLowerCase() === low);
    if (exact.length === 1) return exact[0];
    const partial = nations.filter((n) => String(n['名称'] || '').toLowerCase().includes(low));
    if (partial.length === 1) return partial[0];
    if (partial.length > 1) {
      partial.sort((a, b) => (b['声望'] || 0) - (a['声望'] || 0));
      return { ...partial[0], ambiguous: partial.map((n) => n['名称']) };
    }
    return null;
  }

  /* ---------- 角色上下文 ---------- */

  /**
   * 为一个角色构建世界上下文对象（约 4 KB）。
   * 这是给角色看的"国情简报"，已压缩：只保留最要紧的城市与完整的趋势。
   */
  async contextFor(nationToken) {
    const ident = await this.resolve(nationToken);
    if (!ident) {
      const nations = await this.nations();
      // 分清两种失败：服务连不上，还是国家名写错了。
      // 混在一起会让使用者去查一个不存在的问题。
      if (!nations.length) {
        return {
          error: `世界服务不可用或还没有读到存档（${this._lastError || '未启动'}）。`
            + `请确认已运行 python worldbox_agent.py serve，地址 ${this.baseUrl}`,
        };
      }
      return {
        error: `找不到国家 ${nationToken}`,
        可用国家: nations.map((n) => n['名称']),
      };
    }
    const id = ident.id;
    const [self, others, history, status] = await Promise.all([
      this._get(`/api/v1/nations/${encodeURIComponent(id)}?compact=1`),
      this._get(`/api/v1/nations/${encodeURIComponent(id)}/others?compact=1`),
      this._get(`/api/v1/nations/${encodeURIComponent(id)}/history?limit=10&compact=1`),
      this.status(),
    ]);
    if (!self) return { error: '读不到该国情报（世界服务可能刚断开）' };

    const series = Array.isArray(history && history.series) ? history.series : [];
    const last = series[series.length - 1] || {};
    const first = series[0] || {};
    const trend = (field) => {
      const vals = series.map((r) => r[field]).filter((v) => v !== undefined && v !== null);
      if (!vals.length) return null;
      return { 年份: [first.timestamp, last.timestamp], 数值: vals, 变化: vals[vals.length - 1] - vals[0] };
    };

    const myCities = ((self.territory || {})['城市列表'] || []).slice();
    myCities.sort((a, b) => (b['无家可归'] || 0) - (a['无家可归'] || 0));

    return {
      角色: `你是 WorldBox 世界「${status ? status.worldName : '?'}」中「${ident['名称']}」的领袖。`,
      数据新鲜度: status
        ? {
            世界年份: status.year,
            存档距今秒数: Math.round(status.saveAgeSeconds),
            世界指纹: status.fingerprint,
            警告: status.stale
              ? `⚠️ 数据已 ${(status.saveAgeSeconds / 60).toFixed(1)} 分钟旧，游戏可能已暂停或关闭，请勿当作实时局势`
              : null,
          }
        : { 警告: '⚠️ 世界服务不可用，以下情报可能不是最新的' },
      我的国家: {
        编号: id,
        名称: ident['名称'],
        是否存续: ident['是否存续'],
        国王: (self.ruler || {})['国王'],
        国王特质: ((self.ruler || {})['国王特质'] || []).map((t) => t.label),
        国家特质: ((self.status || {})['国家特质'] || []).map((t) => t.label),
        人口: (self.demographics || {})['人口'],
        军队: (self.military || {})['军队总兵力'],
        城市数: (self.territory || {})['城市数'],
        领土地块: (self.territory || {})['领土地块'],
        声望: (self.status || {})['声望'],
        声望排名: (self.status || {})['声望排名'],
        国库: last.money,
        粮食: last.food,
        饥饿: (self.demographics || {})['饥饿'],
        迁出: (self.status || {})['迁出人数'],
        迁入: (self.status || {})['迁入人数'],
        城市列表_按无家可归排序_仅前8座: myCities.slice(0, 8).map((c) => ({
          名称: c['名称'], 坐标: c['坐标'], 人口: c['人口'],
          士兵: c['其中士兵'], 无家可归: c['无家可归'], 建筑数: c['建筑数'],
        })),
        _城市说明: `共 ${myCities.length} 座城市，这里只列最要紧的 8 座（按无家可归排序）`,
      },
      趋势: {
        人口: trend('population'), 军队: trend('army'),
        国库: trend('money'), 粮食: trend('food'),
        饥饿: trend('hungry'), 无家可归: trend('homeless'),
      },
      我的战争: ((self.diplomacy || {})['战争'] || []).map((w) => ({
        战争: w['战争名称'], 我方角色: w['我方角色'],
        敌方: (w['敌方'] || []).map((o) => o['名称']),
        我方阵亡: w['我方阵亡'], 敌方阵亡: w['敌方阵亡'],
      })),
      列国: ((others || {})['列国'] || []).slice(0, 8).map((n) => ({
        名称: n['名称'], 立场: n['我方立场'], 军队: n['军队兵力'], 人口: n['人口'],
        城市数: n['城市数'], 声望: n['声望'], 国王: n['国王'],
        国王特质: (n['国王特质'] || []).map((t) => t.label),
        国家特质: (n['国家特质'] || []).map((t) => t.label),
        城市列表: (n['城市列表'] || []).map((c) => ({
          名称: c['名称'], 坐标: c['坐标'], 人口: c['人口'], 士兵: c['其中士兵'],
        })),
        领土主张线索: n['领土主张线索'] || undefined,
      })),
      关系矩阵: (others || {})['关系矩阵'] || {},
      近期大事: ((self.recent_events || []).slice(-6)).map((e) => ({
        年份: e['年份'], 事件: e['事件'],
      })),
    };
  }

  /** 给 `world_state` 工具用的纯文本版本（工具结果只能是字符串）。 */
  async stateText(nationToken) {
    const ctx = await this.contextFor(nationToken);
    return JSON.stringify(ctx, null, 1);
  }

  /**
   * 释放缓存。长期运行的服务不需要调用；
   * 但**脚本/测试在结束时应当调用**，否则 fetch 的 keep-alive 连接
   * 会存活到进程退出，在某些 Node 版本上触发 libuv 断言。
   */
  dispose() {
    this._cache.clear();
  }
}

/* ================================================================== */
/* 桥实例管理（每个 baseUrl 一份，房间之间复用）                        */
/* ================================================================== */

const _bridges = new Map();

function getBridge(room) {
  const cfg = (room && room.worldbox) || null;
  if (!cfg || cfg.enabled === false) return null;
  const base = cfg.baseUrl || DEFAULT_BASE;
  if (!_bridges.has(base)) _bridges.set(base, new WorldBridge({ baseUrl: base }));
  return _bridges.get(base);
}

function bridgeEnabled(room) {
  return !!(room && room.worldbox && room.worldbox.enabled !== false);
}

/* ================================================================== */
/* 系统提示注入                                                        */
/* ================================================================== */

/**
 * 生成要拼进 buildMessages() 的"世界局势"段落。
 *
 * 只注入**精简的世界快照 + 该角色所属国家**，约 3–5 KB。
 * 完整国情（27 KB）不注入，让角色按需调用 `world_state` 工具去取。
 *
 * @param {object} room   房间（需有 room.worldbox）
 * @param {object} agent  当前角色
 * @param {object} worldboxBridge  WorldBridge 实例
 */
async function systemSection(room, agent, worldboxBridge) {
  if (!bridgeEnabled(room)) return '';

  const nationToken = resolveNationFor(room, agent);
  if (!nationToken) {
    return [
      '## WorldBox 世界',
      '本房间已接入 WorldBox 世界数据，但**你还没有被指定扮演哪个国家**。',
      '请让管理员在房间设置里把某个国家分配给你（room.worldbox.nationOf）。',
      '在分配之前，不要对世界局势做任何判断。',
    ].join('\n');
  }

  const ctx = await worldboxBridge.contextFor(nationToken);
  if (ctx.error) {
    return [
      '## WorldBox 世界',
      `⚠️ 暂时读不到世界情报：${ctx.error}`,
      '**在拿到情报之前不要对世界局势做判断，也不要编造任何数字。**',
      ctx['可用国家'] && ctx['可用国家'].length
        ? `可用国家：${ctx['可用国家'].join('、')}`
        : '',
    ].filter(Boolean).join('\n');
  }

  const rules = [
    '## WorldBox 世界局势（真实数据，非虚构）',
    '',
    '```json',
    JSON.stringify(ctx, null, 1),
    '```',
    '',
    '## 在这个房间里怎么扮演领袖',
    '',
    '1. **用 worldbox-leader-policy 技能的格式输出《国策》**（零/一/二/三/四/五 六节）：',
    '   - `零、情报状态`：说明数据多旧、缺什么',
    '   - `A. 落点型措施`：必须写成 `执行：请创世者在 <城市名>(x,y) 做什么，数量多少`',
    '   - `B. 开关型措施`：写成 `执行（全国开关）：把 <政策> 调整到 <档位>`',
    '   - `四、内心`：你真实的想法（这一节属于你的内心）',
    '2. **你只能提议，不能执行。** 真正动手的是创世者（玩家）。',
    '   不要说"我已派兵"，要说"请创世者把 X 军移往 (x,y)"。',
    '3. **必须看趋势，不要只看当前一帧。** 上面的 `趋势` 字段才是决策依据：',
    '   人口从峰值跌了一半，比"人口 1767"重要得多。',
    '4. **只依据上面的数据说话。** 数据里没有的字段就说没有，**绝不编造数字**。',
    '5. **先查距离再决定调兵。** 每座城都有坐标，用勾股算一下再决定派谁。',
    '6. 数据超过 10 分钟旧时，明确说明局势可能已变。',
    '7. 需要更细的数据（全部城市、军队番号、逐年历史）时，',
    '   调用 `world_state` 工具，或请管理员直接查看 data/nations/ 下的文件。',
  ];

  if (room.worldbox && room.worldbox.strictTick) {
    rules.push(
      '8. **世界没推进时不要重复决策。** 如果数据新鲜度里的「世界指纹」与上一轮相同，',
      '   只做对话（谈判、结盟、表态），不要产出新的《国策》——局势没变。');
  }

  return rules.join('\n');
}

/** 从房间配置里找出这个角色扮演哪个国家。 */
function resolveNationFor(room, agent) {
  const cfg = (room && room.worldbox) || {};
  if (cfg.nationOf && typeof cfg.nationOf === 'object') {
    const hit = cfg.nationOf[agent.name] || cfg.nationOf[agent.id];
    if (hit) return hit;
  }
  if (agent && agent.worldboxNation) return agent.worldboxNation;
  if (cfg.nation) return cfg.nation;    // 单角色房间的简写
  return null;
}

/**
 * 决定某个角色最终应该用哪段人设作为 system prompt 的开头。
 *
 * 优先级（高 → 低）：
 *   1. 使用者手写的 `systemPrompt`
 *   2. 自动生成的 `systemPromptAuto`（由 autofillSystemPrompts 写入）
 *   3. 空串
 *
 * 两者都存在时**都保留**：手写的是使用者对角色的额外要求，
 * 自动生成的是客观国情，拼在一起信息最全，而且使用者一眼能看出
 * 哪段是自己写的、哪段是系统补的。
 */
function rolePromptFor(agent) {
  const explicit = String((agent && agent.systemPrompt) || '').trim();
  const auto = String((agent && agent.systemPromptAuto) || '').trim();
  if (explicit && auto) return `${explicit}\n\n${auto}`;
  return explicit || auto || '';
}

/* ================================================================== */
/* 自动填充角色 system prompt                                          */
/* ================================================================== */

/**
 * 根据当前世界状态，自动为 WorldBox 房间里还没有人设的角色生成 system prompt。
 *
 * 为什么需要：新建一个"世界频道"房间时，如果每个角色的 system prompt 都是空的，
 * 它们只会泛泛而谈，不会扮演领袖。而正确的 prompt 其实**完全由数据决定**：
 * 国名、世界名、国王与其特质、国家特质、国力地位——这些都在情报里，
 * 让使用者手抄一遍既枯燥又容易抄错。
 *
 * 设计约束（很重要）：
 *
 * 1. **绝不覆盖已有的人设。** 使用者写过的 prompt 是他对这个角色的设定，
 *    优先级永远高于自动生成的内容。只填空白的。
 * 2. **只在 WorldBox 房间里做。** 普通房间返回 0，不做任何事。
 * 3. **写进一个独立字段 systemPromptAuto，不动 systemPrompt。**
 *    这样使用者一眼能看出哪段是自动生成的，想改随时能改，
 *    而且清空它就能重新生成。
 * 4. **世界换了要重新生成。** 记录生成时的存档指纹；指纹变了说明换了存档/
 *    世界推进到了新世界，旧的国名与国王可能已不存在，必须重算。
 *
 * @returns {Promise<{filled:number, refreshed:number, skipped:number, details:Array}>}
 */
async function autofillSystemPrompts(room, bridge, opts = {}) {
  const result = { filled: 0, refreshed: 0, skipped: 0, details: [] };
  if (!bridgeEnabled(room)) return result;
  if (!bridge) return result;

  const status = await bridge.status();
  if (!status) {
    result.error = '世界服务不可用，无法生成人设';
    return result;
  }
  const fingerprint = status.fingerprint;
  const nations = await bridge.nations();
  if (!nations.length) {
    result.error = '读不到国家列表，无法生成人设';
    return result;
  }
  const byName = new Map(nations.map((n) => [String(n['名称']), n]));

  for (const agent of room.agents || []) {
    const token = resolveNationFor(room, agent);
    if (!token) { result.skipped++; result.details.push({ agent: agent.name, action: '跳过', reason: '未指定国家' }); continue; }

    const nation = byName.get(String(token))
      || nations.find((n) => String(n['名称']).includes(String(token)));
    if (!nation) { result.skipped++; result.details.push({ agent: agent.name, action: '跳过', reason: `找不到国家 ${token}` }); continue; }

    const explicit = String(agent.systemPrompt || '').trim();
    const generated = String(agent.systemPromptAuto || '').trim();
    const stale = agent.systemPromptAutoFor && agent.systemPromptAutoFor !== fingerprint;

    // 使用者自己写过人设，且没有要求强制覆盖 → 尊重他
    if (explicit && !opts.force) {
      result.skipped++;
      result.details.push({ agent: agent.name, action: '保留',
                            reason: '已有人设，未被覆盖' });
      continue;
    }
    if (generated && !stale && !opts.force) {
      result.skipped++;
      result.details.push({ agent: agent.name, action: '保留', reason: '已生成且世界未变' });
      continue;
    }

    const ctx = await bridge.contextFor(nation['名称']);
    if (ctx.error) { result.skipped++; result.details.push({ agent: agent.name, action: '跳过', reason: ctx.error }); continue; }

    agent.systemPromptAuto = composePrompt(ctx, status, nation);
    agent.systemPromptAutoFor = fingerprint;
    agent.systemPromptAutoAt = Date.now();

    if (stale) { result.refreshed++; result.details.push({ agent: agent.name, action: '重新生成', reason: '世界已变化' }); }
    else { result.filled++; result.details.push({ agent: agent.name, action: '生成' }); }
  }
  return result;
}

/** 把世界数据写成一个角色能直接用的中文人设。 */
function composePrompt(ctx, status, nation) {
  const me = ctx['我的国家'] || {};
  const trend = ctx['趋势'] || {};
  const pop = (trend['人口'] || {}).变化;
  const army = (trend['军队'] || {}).变化;

  const traitText = (me['国王特质'] || []).length
    ? `你的性格倾向是：${me['国王特质'].join('、')}。`
    : '';
  const nationTraitText = (me['国家特质'] || []).length
    ? `你的国家特质是：${me['国家特质'].join('、')}。`
    : '';

  // 用趋势给一点"处境感"，让角色一开口就有立场
  let situation = '';
  if (typeof pop === 'number' && pop < 0) {
    situation = `你的国家正在衰落（近年人口变化 ${pop}），你对每一分损失都很敏感。`;
  } else if (typeof pop === 'number' && pop > 0) {
    situation = `你的国家正在恢复（近年人口变化 +${pop}），你倾向于稳步扩张。`;
  }
  if (typeof army === 'number' && army < 0) {
    situation += '你的军队在缩水，你不愿轻易开启新的战争。';
  }

  const wars = (ctx['我的战争'] || []);
  const warText = wars.length
    ? `你目前正在打 ${wars.length} 场战争：${wars.map((w) => `对${(w.敌方 || []).join('、')}`).join('；')}。`
    : '你目前没有在打仗。';

  const rivals = (ctx['列国'] || []).slice(0, 4)
    .map((n) => `${n['名称']}（军队 ${n['军队']}，人口 ${n['人口']}，与你是${n['立场']}）`)
    .join('；');

  return [
    `你是 WorldBox 世界「${status.worldName}」中「${me['名称']}」的国王，你的名字是「${me['国王'] || '（未记录）'}」。`,
    `世界当前是第 ${status.year} 年，你的国家有 ${me['人口']} 人口、${me['军队']} 军队、${me['城市数']} 座城市、领土地块 ${me['领土地块']}，声望 ${me['声望']}（第 ${me['声望排名']} 名）。`,
    traitText + nationTraitText,
    situation,
    warText,
    rivals ? `你周边的国家：${rivals}。` : '',
    '',
    '你是一个真实历史里的君主，不是助手。你有自己的野心、恐惧和执念。',
    '做任何决策前先看平台给你的世界局势数据，尤其是**趋势**——单看当前一帧说明不了问题。',
    '你只能提出政策，实际执行者是创世者（玩家）。',
    '数据里没有的东西就说没有，绝不编造数字。',
  ].filter(Boolean).join('\n');
}

module.exports = {
  TOOL_DEF,
  WorldBridge,
  getBridge,
  bridgeEnabled,
  systemSection,
  resolveNationFor,
  autofillSystemPrompts,
  composePrompt,
  rolePromptFor,
  DEFAULT_BASE,
  STALE_AFTER_SECONDS,
};
