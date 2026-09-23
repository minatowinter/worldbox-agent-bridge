#!/usr/bin/env node
'use strict';

/**
 * Agent Room —— 多 Agent / 多房间协作环境（零依赖）
 * ------------------------------------------------------------
 *  · 多个房间，各自独立：房间设定、成员、对话记录、内心日志、工作区目录
 *  · 每个 Agent 可单独配置模型 / 接口 / 人设，并可选是否允许修改文件
 *  · 三种发言模式：轮流发言 / 按需发言（Agent 自己判断要不要说话）/ 手动点名
 *  · Agent 的思考与工具过程记录在各自的「内心」，房间里只出现正式发言
 *  · 工作区：room/room1、room/room2 … 每个房间一个独立文件夹
 */

const http = require('node:http');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const worldbox = require('./worldbox-bridge.js');   // WorldBox 世界接入桥

const ROOT = __dirname;
const PUBLIC_DIR = path.join(ROOT, 'public');
const DATA_DIR = path.join(ROOT, 'data');
const TRANSCRIPT_DIR = path.join(DATA_DIR, 'transcripts');
const ROOMS_ROOT = path.join(ROOT, 'room');      // 工作区根目录
const LEGACY_SHARED = path.join(ROOT, 'shared'); // 旧版工作区（会自动迁移）
const CONFIG_PATH = path.join(DATA_DIR, 'config.json');
const PORT = Number(process.env.PORT || 8787);
const MAX_BODY = 4 * 1024 * 1024;
const MAX_FILE_BYTES = 256 * 1024;
const MAX_INNER = 300;   // 每个 Agent 内心日志上限
const MAX_ROOM_MSGS = 800;

/* ================================================================== */
/* 数据模型                                                           */
/* ================================================================== */

const COLORS = ['#7c9cff', '#4ec9b0', '#e2b04a', '#e06c9f', '#9d7cff', '#5ac8fa', '#ff8a65', '#a3d977'];

function uid(prefix = 'a') {
  return prefix + '_' + crypto.randomBytes(4).toString('hex');
}

function defaultAgents() {
  return [
    {
      id: uid(),
      name: '小周',
      emoji: '🧭',
      color: COLORS[0],
      model: 'deepseek-chat',
      apiBase: 'https://api.deepseek.com/v1',
      apiKey: '',
      temperature: 0.8,
      enabled: true,
      canModifyFiles: true,
      systemPrompt:
        '你是小周，产品经理。你负责推进讨论、把模糊的需求收敛成明确的结论，并指派下一步。' +
        '你说话简短、结构化、有结论。需要别人接手时用 @名字 点名。',
    },
    {
      id: uid(),
      name: '老陈',
      emoji: '🛠️',
      color: COLORS[1],
      model: 'deepseek-chat',
      apiBase: 'https://api.deepseek.com/v1',
      apiKey: '',
      temperature: 0.4,
      enabled: true,
      canModifyFiles: true,
      systemPrompt:
        '你是老陈，工程师。你务实、直接，对不合理的需求会明确说"不"并给出替代方案。' +
        '你负责把方案落地成共享工作区里真实、可运行的文件——不要只说"我会写"，直接调用 write_file。',
    },
    {
      id: uid(),
      name: 'May',
      emoji: '🙋',
      color: COLORS[2],
      model: 'deepseek-chat',
      apiBase: 'https://api.deepseek.com/v1',
      apiKey: '',
      temperature: 0.9,
      enabled: true,
      canModifyFiles: false,
      systemPrompt:
        '你是 May，代表真实用户。你不懂技术细节，只关心"这东西对我有什么用、会不会让我困惑"。' +
        '你擅长挑毛病、提反例和抱怨，说话口语化。你可以看工作区里的文件，但从不改文件。',
    },
  ];
}

function defaultRoom(name, dir) {
  return {
    id: uid('r'),
    name,
    dir,
    setting:
      '这是一场产品评审会。小周是产品经理，负责推进需求；老陈是工程师，负责实现并对不合理的需求说不；' +
      'May 代表真实用户，负责挑毛病。三个人要围绕"下一版做什么"聊出一个可执行的结论。',
    mode: 'round_robin',
    rounds: 2,
    maxTurns: 40,
    maxToolSteps: 4,
    followMentions: true,
    temperature: 0.7,
    historyLimit: 60,
    agents: defaultAgents(),
  };
}

/* ---------------- WorldBox 世界房间：配置与默认角色 ---------------- */

const WORLDBOX_SETTING =
  '这是 WorldBox 世界里各国君主的议政厅。各国领袖在此谈判、结盟、宣战、救灾，' +
  '并把决策写成《国策》交给"创世者"（玩家）在游戏里执行。' +
  '你只能提议，不能执行；世界每年推进一次，数据以最新存档为准。';

function normalizeWorldbox(cfg) {
  if (!cfg || cfg.enabled === false) return null;
  const cap = Number(cfg.maxActionsPerTurn);
  return {
    enabled: true,
    baseUrl: String(cfg.baseUrl || worldbox.DEFAULT_BASE),
    strictTick: cfg.strictTick !== false,
    maxActionsPerTurn: Number.isFinite(cap) && cap > 0 ? Math.min(9, Math.floor(cap)) : 3,
    nationOf: (cfg.nationOf && typeof cfg.nationOf === 'object') ? { ...cfg.nationOf } : {},
    lastFingerprint: cfg.lastFingerprint || null,
    // 锁定使用哪一份存档。null = 跟随最新。
    // 一个正常玩家有几十个存档，分属不同世界与时间，所以这必须是显式可选项，
    // 否则房间会被接到一个使用者根本不关心的世界上。
    saveKey: cfg.saveKey ? String(cfg.saveKey) : null,
  };
}

/** 由世界数据生成一个国家领袖角色。 */
function leaderAgent(nation, i) {
  const name = nation['名称'];
  return normalizeAgent({
    id: uid(),
    name,
    emoji: '👑',
    color: COLORS[(i + 4) % COLORS.length],
    model: 'deepseek-chat',
    apiBase: 'https://api.deepseek.com/v1',
    apiKey: '',
    temperature: 0.85,
    enabled: true,
    canModifyFiles: true,
    systemPrompt:
      `你是 WorldBox 世界「${nation['名称']}」的领袖。你只为本国利益行事：` +
      '先看清本国的人口、军队、国库、城市与逐年趋势，再决定战与和。' +
      '你有自己的性格与执念（谨慎、记仇、好战或爱民），不轻信邻国，也不替别国着想。' +
      '你只能向创世者提交《国策》，不能自己动手执行。' +
      '你治下的国家经不起折腾：每轮只做少量、小步、可逆的调整，' +
      '一次决策只解决当下最紧的那件事，不要试图一轮改造整个国家。' +
      '动笔前先 read_file 你的 memory.md，决策后把这一年 append_file 进去。',
  });
}

function placeholderLeaders() {
  return ['领袖一号', '领袖二号', '领袖三号'].map((name, i) =>
    normalizeAgent({
      id: uid(), name, emoji: '👑', color: COLORS[(i + 4) % COLORS.length],
      model: 'deepseek-chat', apiBase: 'https://api.deepseek.com/v1', apiKey: '',
      temperature: 0.85, enabled: true, canModifyFiles: true,
      systemPrompt:
        `你是 WorldBox 世界里「${name}」的领袖。你还不知道自己统治哪个国家——` +
        '先向创世者确认你代表哪一个国家，再开始决策。你只为本国利益行事。',
    })
  );
}

/** 运行时全局状态（进程级） */
const state = {
  rooms: [],           // [{...cfg, messages, inner, stats, queue, currentAgentId}]
  activeRoomId: null,
  runner: { running: false, roomId: null, stopRequested: false, abort: null },
  startedAt: Date.now(),
};

const sseClients = new Set();

function log(...a) { console.log('[agent-room]', ...a); }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function truncate(s, n) { s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s; }

/* ================================================================== */
/* 房间工具                                                           */
/* ================================================================== */

function activeRoom() {
  return state.rooms.find((r) => r.id === state.activeRoomId) || null;
}
function roomById(id) { return state.rooms.find((r) => r.id === id) || null; }
function roomPath(room, rel = '') {
  const abs = path.resolve(ROOMS_ROOT, room.dir, rel);
  const r = path.relative(path.join(ROOMS_ROOT, room.dir), abs);
  if (r.startsWith('..') || path.isAbsolute(r)) throw new Error('路径越界，只能访问本房间的工作区');
  return abs;
}
function safePath(room, rel) {
  const cleaned = String(rel == null ? '' : rel).replace(/^[/\\]+/, '').trim();
  if (!cleaned) throw new Error('path 不能为空');
  if (cleaned.includes('\0')) throw new Error('非法路径');
  const abs = roomPath(room, cleaned);
  return { abs, rel: path.relative(path.join(ROOMS_ROOT, room.dir), abs).split(path.sep).join('/') };
}
function nextRoomDir(kind) {
  // 既避开在用的房间，也避开磁盘上遗留的目录
  // （删房间时我们有意保留 room/<dir>/，所以不能只查内存，否则新房间会继承旧房间的文件）
  const taken = (d) => state.rooms.some((r) => r.dir === d) || fs.existsSync(path.join(ROOMS_ROOT, d));
  if (kind === 'worldbox') {
    if (!taken('worldbox')) return 'worldbox';
    let i = 2;
    while (taken('worldbox' + i)) i++;
    return 'worldbox' + i;
  }
  let n = 1;
  while (taken('room' + n)) n++;
  return 'room' + n;
}

/* ================================================================== */
/* 内心日志                                                           */
/* ================================================================== */

function pushInner(room, agent, kind, payload) {
  const list = (room.inner[agent.id] ||= []);
  const entry = { id: uid('i'), ts: Date.now(), agentId: agent.id, agentName: agent.name, kind, ...payload };
  if (entry.text != null) entry.text = truncate(entry.text, 4000);
  list.push(entry);
  if (list.length > MAX_INNER) list.splice(0, list.length - MAX_INNER);
  broadcast('inner', { roomId: room.id, agentId: agent.id, entry: publicInner(entry) });
  return entry;
}
function publicInner(e) {
  return { id: e.id, ts: e.ts, agentId: e.agentId, agentName: e.agentName, kind: e.kind,
           text: e.text, tool: e.tool, meta: e.meta };
}

/* ================================================================== */
/* 广播                                                               */
/* ================================================================== */

function broadcast(event, data) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of sseClients) {
    try { res.write(payload); } catch { sseClients.delete(res); }
  }
}

/* ================================================================== */
/* 序列化                                                             */
/* ================================================================== */

function resolveSecret(value) {
  if (!value) return '';
  return String(value).replace(/\$\{ENV:([A-Z0-9_]+)\}/gi, (_, n) => process.env[n] || '');
}

function publicAgent(room, a) {
  const st = (room.stats[a.id] ||= { turns: 0, promptTokens: 0, completionTokens: 0, errors: 0 });
  return {
    id: a.id, name: a.name, emoji: a.emoji, color: a.color, model: a.model, apiBase: a.apiBase,
    temperature: a.temperature, enabled: a.enabled, canModifyFiles: a.canModifyFiles !== false,
    systemPrompt: a.systemPrompt, hasKey: Boolean(resolveSecret(a.apiKey)), stats: st,
    innerCount: (room.inner[a.id] || []).length,
    worldboxNation: worldbox.resolveNationFor(room, a) || null,
    // 由世界数据自动生成的人设。前端要显示它、也要能清空重生成，
    // 所以必须出现在公开数据里——否则界面永远看不到它，用户会以为没生成。
    systemPromptAuto: a.systemPromptAuto || '',
    systemPromptAutoFor: a.systemPromptAutoFor || null,
    // 这个角色最终实际生效的人设（手写 + 自动生成），供界面预览
    effectivePrompt: worldbox.rolePromptFor(a) || a.systemPrompt || '',
  };
}

function publicRoom(room) {
  const proposed = room.actions.filter((a) => a.status === 'proposed').length;
  return {
    id: room.id, kind: room.kind || 'normal', name: room.name, dir: room.dir,
    setting: room.setting, mode: room.mode,
    rounds: room.rounds, maxTurns: room.maxTurns, maxToolSteps: room.maxToolSteps,
    followMentions: room.followMentions, temperature: room.temperature, historyLimit: room.historyLimit,
    messageCount: room.messages.length,
    worldbox: room.worldbox
      ? { enabled: room.worldbox.enabled, baseUrl: room.worldbox.baseUrl, strictTick: room.worldbox.strictTick,
          maxActionsPerTurn: room.worldbox.maxActionsPerTurn,
          saveKey: room.worldbox.saveKey || null,
          nationOf: room.worldbox.nationOf, lastFingerprint: room.worldbox.lastFingerprint }
      : null,
    // 人设是否已按世界数据生成（前端用来显示"未生成人设"的提示）
    promptAuto: (room.agents || []).filter((a) => a.systemPromptAuto).length,
    promptManual: (room.agents || []).filter((a) => String(a.systemPrompt || '').trim()).length,
    actionCounts: {
      proposed,
      unparsed: room.actions.filter((a) => a.status === 'unparsed').length,
      executed: room.actions.filter((a) => a.status === 'executed').length,
      rejected: room.actions.filter((a) => a.status === 'rejected').length,
      obsolete: room.actions.filter((a) => a.status === 'obsolete').length,
    },
    agents: room.agents.map((a) => publicAgent(room, a)),
  };
}

function publicRoomFull(room) {
  return {
    ...publicRoom(room),
    messages: room.messages,
    inner: Object.fromEntries(Object.entries(room.inner).map(([k, v]) => [k, v.map(publicInner)])),
    queue: room.queue.slice(),
    currentAgentId: room.currentAgentId,
    running: state.runner.running && state.runner.roomId === room.id,
  };
}

function publicGlobal() {
  return {
    activeRoomId: state.activeRoomId,
    rooms: state.rooms.map(publicRoom),
    running: state.runner.running,
    runningRoomId: state.runner.roomId,
    startedAt: state.startedAt,
  };
}

/* ================================================================== */
/* 持久化                                                             */
/* ================================================================== */

let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => doSave().catch((e) => log('保存失败', e)), 400);
}

async function doSave() {
  await fsp.mkdir(DATA_DIR, { recursive: true });
  await fsp.mkdir(TRANSCRIPT_DIR, { recursive: true });
  const cfg = {
    version: 2,
    activeRoomId: state.activeRoomId,
    rooms: state.rooms.map((r) => ({
      id: r.id, kind: r.kind, name: r.name, dir: r.dir, setting: r.setting, mode: r.mode, rounds: r.rounds,
      maxTurns: r.maxTurns, maxToolSteps: r.maxToolSteps, followMentions: r.followMentions,
      temperature: r.temperature, historyLimit: r.historyLimit, agents: r.agents,
      worldbox: r.worldbox ? { ...r.worldbox, lastFingerprint: r.worldbox.lastFingerprint } : null,
    })),
  };
  await fsp.writeFile(CONFIG_PATH, JSON.stringify(cfg, null, 2), 'utf8');
  for (const r of state.rooms) {
    await fsp.writeFile(
      path.join(TRANSCRIPT_DIR, r.id + '.json'),
      JSON.stringify({
        messages: r.messages.slice(-MAX_ROOM_MSGS), inner: r.inner, stats: r.stats,
        actions: r.actions.slice(-400),
      }),
      'utf8'
    );
  }
}

function normalizeAgent(a, i) {
  return {
    id: a.id || uid(),
    name: a.name || `Agent ${i + 1}`,
    emoji: a.emoji || '🤖',
    color: a.color || COLORS[i % COLORS.length],
    model: a.model || 'deepseek-chat',
    apiBase: a.apiBase || 'https://api.deepseek.com/v1',
    apiKey: a.apiKey || '',
    temperature: typeof a.temperature === 'number' ? a.temperature : 0.7,
    enabled: a.enabled !== false,
    canModifyFiles: a.canModifyFiles !== false,
    // 注意：这里**不**给 WorldBox 领袖塞兜底人设。
    // 若系统替它编一句"你是一个有自己立场和想法的角色"，就会被
    // autofillSystemPrompts 当成"使用者手写过人设"而拒绝生成真实国情。
    systemPrompt: a.systemPrompt || '',
    // 由世界数据自动生成的人设，与手写的人设分开存放，
    // 这样使用者一眼能看出哪段是系统补的，清空即可重新生成。
    systemPromptAuto: a.systemPromptAuto || '',
    systemPromptAutoFor: a.systemPromptAutoFor || null,
    systemPromptAutoAt: a.systemPromptAutoAt || null,
    worldboxNation: a.worldboxNation || null,
  };
}

function normalizeRoom(c) {
  const kind = c.kind === 'worldbox' ? 'worldbox' : 'normal';
  return {
    id: c.id || uid('r'),
    kind,
    name: c.name || '未命名房间',
    dir: c.dir || 'room1',
    setting: c.setting || c.goal || '',
    mode: ['round_robin', 'on_demand', 'manual'].includes(c.mode) ? c.mode : 'round_robin',
    rounds: Number(c.rounds) || 2,
    maxTurns: Number(c.maxTurns) || 40,
    maxToolSteps: Number(c.maxToolSteps) || 4,
    followMentions: c.followMentions !== false,
    temperature: typeof c.temperature === 'number' ? c.temperature : 0.7,
    historyLimit: Number(c.historyLimit) || 60,
    worldbox: normalizeWorldbox(kind === 'worldbox' ? (c.worldbox || { enabled: true }) : c.worldbox),
    agents: (Array.isArray(c.agents) && c.agents.length ? c.agents : defaultAgents()).map(normalizeAgent),
    messages: [], inner: {}, stats: {}, actions: [], queue: [], currentAgentId: null,
  };
}

async function loadAll() {
  await fsp.mkdir(DATA_DIR, { recursive: true });
  await fsp.mkdir(TRANSCRIPT_DIR, { recursive: true });
  await fsp.mkdir(ROOMS_ROOT, { recursive: true });

  let cfg = null;
  try { cfg = JSON.parse(await fsp.readFile(CONFIG_PATH, 'utf8')); } catch { cfg = null; }

  let rawRooms;
  if (cfg && Array.isArray(cfg.rooms)) {
    rawRooms = cfg.rooms;
    state.activeRoomId = cfg.activeRoomId || null;
  } else if (cfg && Array.isArray(cfg.agents)) {
    // v1 迁移：单房间配置 → 多房间
    const legacy = defaultRoom('房间 1', 'room1');
    if (cfg.room) {
      legacy.setting = cfg.room.goal || legacy.setting;
      legacy.mode = cfg.room.mode || legacy.mode;
      legacy.rounds = cfg.room.rounds ?? legacy.rounds;
      legacy.maxTurns = cfg.room.maxTurns ?? legacy.maxTurns;
      legacy.maxToolSteps = cfg.room.maxToolSteps ?? legacy.maxToolSteps;
      legacy.followMentions = cfg.room.followMentions ?? legacy.followMentions;
      legacy.temperature = cfg.room.temperature ?? legacy.temperature;
      legacy.historyLimit = cfg.room.historyLimit ?? legacy.historyLimit;
    }
    legacy.agents = cfg.agents.map(normalizeAgent);
    rawRooms = [legacy];
    state.activeRoomId = legacy.id;
    log('检测到 v1 配置，已迁移为多房间结构');
  } else {
    const r1 = defaultRoom('房间 1', 'room1');
    rawRooms = [r1];
    state.activeRoomId = r1.id;
  }

  state.rooms = rawRooms.map(normalizeRoom);
  if (!state.rooms.length) {
    const r1 = defaultRoom('房间 1', 'room1');
    state.rooms.push(normalizeRoom(r1));
    state.activeRoomId = r1.id;
  }
  if (!state.rooms.some((r) => r.id === state.activeRoomId)) state.activeRoomId = state.rooms[0].id;

  // 载入每个房间的记录
  for (const r of state.rooms) {
    try {
      const t = JSON.parse(await fsp.readFile(path.join(TRANSCRIPT_DIR, r.id + '.json'), 'utf8'));
      r.messages = Array.isArray(t.messages) ? t.messages : [];
      r.inner = (t.inner && typeof t.inner === 'object') ? t.inner : {};
      r.stats = (t.stats && typeof t.stats === 'object') ? t.stats : {};
      r.actions = Array.isArray(t.actions) ? t.actions : [];
    } catch { /* 新房间 */ }
    if (!r.messages.length && Array.isArray(cfg?.messages) && state.rooms.length === 1) {
      r.messages = cfg.messages; // v1 transcript 兼容
    }
    await fsp.mkdir(roomPath(r), { recursive: true });
  }

  // 旧 shared/ → room/room1/ 迁移
  const first = state.rooms[0];
  try {
    const legacyFiles = (await fsp.readdir(LEGACY_SHARED)).filter((f) => !f.startsWith('.'));
    const target = roomPath(first);
    const targetFiles = (await fsp.readdir(target)).filter((f) => !f.startsWith('.'));
    if (legacyFiles.length && !targetFiles.length) {
      for (const f of legacyFiles) {
        await fsp.cp(path.join(LEGACY_SHARED, f), path.join(target, f), { recursive: true });
      }
      await fsp.rm(LEGACY_SHARED, { recursive: true, force: true });
      log(`已将旧 shared/ 的内容迁移到 room/${first.dir}/`);
    } else if (!legacyFiles.length) {
      // 旧目录已经空了，清掉这个残留目录
      await fsp.rm(LEGACY_SHARED, { recursive: true, force: true });
      log('已清理空的旧工作区 shared/');
    }
  } catch { /* 没有旧目录 */ }

  await doSave();
}

/* ================================================================== */
/* 工具：房间工作区                                                   */
/* ================================================================== */

async function listFiles(room) {
  const base = roomPath(room);
  const out = [];
  async function walk(dir, prefix) {
    let entries = [];
    try { entries = await fsp.readdir(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (e.name.startsWith('.')) continue;
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (e.isDirectory()) await walk(path.join(dir, e.name), rel);
      else {
        const st = await fsp.stat(path.join(dir, e.name)).catch(() => null);
        out.push({ path: rel, size: st ? st.size : 0, mtime: st ? st.mtimeMs : 0 });
      }
    }
  }
  await walk(base, '');
  return out.sort((a, b) => a.path.localeCompare(b.path));
}

const TOOLS = {
  list_files: {
    write: false,
    desc: '列出本房间工作区中的所有文件（路径 + 大小）。参数：{}',
    run: async (room) => {
      const files = await listFiles(room);
      if (!files.length) return '（本房间工作区当前为空）';
      return files.map((f) => `${f.path}  (${f.size} bytes)`).join('\n');
    },
  },
  read_file: {
    write: false,
    desc: '读取工作区中的文件。参数：{"path": "相对路径"}',
    run: async (room, args) => {
      const { abs, rel } = safePath(room, args.path);
      const text = await fsp.readFile(abs, 'utf8');
      return `--- ${rel} ---\n${text.length > 20000 ? text.slice(0, 20000) + '\n…(已截断)' : text}`;
    },
  },
  write_file: {
    write: true,
    desc: '写入（覆盖）工作区中的文件，目录会自动创建。参数：{"path": "相对路径", "content": "完整内容"}',
    run: async (room, args) => {
      const { abs, rel } = safePath(room, args.path);
      const content = String(args.content ?? '');
      if (Buffer.byteLength(content, 'utf8') > MAX_FILE_BYTES) throw new Error(`内容过大（上限 ${MAX_FILE_BYTES} 字节）`);
      await fsp.mkdir(path.dirname(abs), { recursive: true });
      await fsp.writeFile(abs, content, 'utf8');
      broadcast('workspace', { roomId: room.id, action: 'write', path: rel });
      return `已写入 ${rel}（${Buffer.byteLength(content, 'utf8')} 字节）`;
    },
  },
  append_file: {
    write: true,
    desc: '在文件末尾追加内容（不存在则创建）。参数：{"path": "相对路径", "content": "要追加的内容"}',
    run: async (room, args) => {
      const { abs, rel } = safePath(room, args.path);
      await fsp.mkdir(path.dirname(abs), { recursive: true });
      await fsp.appendFile(abs, String(args.content ?? ''), 'utf8');
      broadcast('workspace', { roomId: room.id, action: 'write', path: rel });
      return `已追加到 ${rel}`;
    },
  },
  delete_file: {
    write: true,
    desc: '删除工作区中的文件。参数：{"path": "相对路径"}',
    run: async (room, args) => {
      const { abs, rel } = safePath(room, args.path);
      await fsp.unlink(abs);
      broadcast('workspace', { roomId: room.id, action: 'delete', path: rel });
      return `已删除 ${rel}`;
    },
  },
  // WorldBox 世界情报：只在启用了 worldbox 的房间里出现（见 toolProtocolFor）
  world_state: worldbox.TOOL_DEF,
};

function toolProtocolFor(agent, room) {
  const canWrite = agent.canModifyFiles !== false;
  const available = Object.entries(TOOLS).filter(([n, t]) =>
    (canWrite || !t.write)
    && (n !== 'world_state' || worldbox.bridgeEnabled(room)));
  const lines = available.map(([n, t]) => `- ${n}：${t.desc}`).join('\n');
  if (!canWrite) {
    return `## 你可以使用的工具
你在这个房间里是**只读**的：可以查看工作区，但不能创建、修改或删除任何文件。

<tool name="工具名">
{"参数": "值"}
</tool>

可用工具：
${lines}

规则：
1. 工具块必须单独成段，JSON 必须合法（字符串里的换行写成 \\n）。
2. 不要把工具块里的内容当作发言；真正想说的话写在工具块外面。
3. 只能使用上面这一种工具格式。不要输出任何其它工具协议标记、函数调用语法或内部标记。
4. 如果别人请你改文件，说明你不能改，并让有权限的人去做。`;
  }
  return `## 你可以使用的工具（协作能力）
你不只是聊天，你可以操作本房间的共享工作区。需要时在回复里单独写出工具调用块：

<tool name="工具名">
{"参数": "值"}
</tool>

可用工具：
${lines}

规则：
1. 一次可以写多个 <tool> 块，系统会按顺序执行并把结果回给你；随后你可以继续下一步或给出最终答复。
2. 工具块必须单独成段，JSON 必须合法（字符串里的换行要写成 \\n）。
3. 不要把工具块里的内容当作发言；真正想说的话写在工具块外面。
4. 只能使用上面这一种工具格式。不要输出任何其它工具协议标记、函数调用语法或内部标记。
5. 声称完成了某件事之前，必须真的调用过对应工具。
6. 用 @名字 可以把话头递给房间里的其他人。`;
}

/* ================================================================== */
/* 解析模型输出                                                       */
/* ================================================================== */

/**
 * 归一化模型的「原生工具标记」。
 *
 * DeepSeek 系模型有时会把它内部的函数调用标记直接泄漏到正文里，
 * 而不是按我们规定的格式输出。这里先把这些标记统一成干净的 XML 标签，
 * 后面就能用同一套逻辑解析与剥离。
 *
 * 各家用的竖线字符不统一（半角、全角、竖线变体），斜杠也可能出现在标记名的
 * 前后两侧，所以正则都做了容错：只要尖括号里出现带竖线的标记关键字就归一化。
 */
const NATIVE_MARK = 'D' + 'SML';   // 拆开写，避免源码里出现完整的标记字样
const NATIVE_TAG_RE = new RegExp(
  '<\\s*(\\/?)\\s*[|｜丨]{1,4}\\s*' + NATIVE_MARK + '\\s*[|｜丨]{1,4}\\s*(\\/?)\\s*([a-zA-Z_][\\w-]*)',
  'gi'
);

function normalizeModelMarkup(text) {
  return String(text || '').replace(NATIVE_TAG_RE, (m, before, after, tag) =>
    '<' + (before || after ? '/' : '') + tag);
}

/** 参数值里的 HTML 实体还原成普通字符。 */
function decodeEntities(s) {
  return String(s ?? '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

function parseToolCalls(text) {
  const raw = String(text || '');
  const calls = [];
  const push = (name, rawArgs) => {
    const trimmed = String(rawArgs || '').trim();
    let args = {};
    if (trimmed) {
      try { args = JSON.parse(trimmed); }
      catch {
        try { args = JSON.parse(trimmed.replace(/,\s*([}\]])/g, '$1')); }
        catch { calls.push({ name, args: null, raw: trimmed, parseError: true }); return; }
      }
    }
    calls.push({ name, args, raw: trimmed });
  };

  // 1) 本项目规定的格式
  let m;
  const xmlRe = /<tool\s+name\s*=\s*["']([^"']+)["']\s*>([\s\S]*?)<\/tool>/gi;
  while ((m = xmlRe.exec(raw))) push(m[1].trim(), m[2]);

  // 2) 围栏格式
  const fenceRe = /```(?:tool|json)?[:\s]*([a-z_]+)\s*\n([\s\S]*?)```/gi;
  while ((m = fenceRe.exec(raw))) if (TOOLS[m[1].trim()]) push(m[1].trim(), m[2]);

  // 3) 模型原生标记（归一化后按 invoke / parameter 解析）
  const norm = normalizeModelMarkup(raw);
  // 闭合写法各家不同（有的带斜杠、有的不带），所以不依赖闭合标签：
  // 用前瞻切出每段 invoke 主体，再按 parameter 开标签切片取参数值。
  const invokeRe = /<invoke\s+name\s*=\s*["']([^"']+)["']\s*>([\s\S]*?)(?=<\/?invoke\s*>|<invoke\s+name\s*=\s*["']|$)/gi;
  const seen = new Set();
  let im;
  while ((im = invokeRe.exec(norm))) {
    const name = im[1].trim();
    if (!TOOLS[name]) continue;
    const body = im[2];
    const args = {};

    const parts = [];
    const paramOpenRe = /<parameter\s+name\s*=\s*["']([^"']+)["']\s*>/gi;
    let pm;
    while ((pm = paramOpenRe.exec(body))) {
      parts.push({ name: pm[1].trim(), open: pm.index, contentStart: paramOpenRe.lastIndex });
    }
    for (let i = 0; i < parts.length; i++) {
      const end = i + 1 < parts.length ? parts[i + 1].open : body.length;
      args[parts[i].name] = decodeEntities(
        body.slice(parts[i].contentStart, end)
          .replace(/<\/?parameter\s*>/gi, '')
          .replace(/^\n+|\n+$/g, '')
      );
    }
    if (!Object.keys(args).length && body.trim()) {
      try { Object.assign(args, JSON.parse(body.trim())); } catch { /* 保持空参数 */ }
    }
    const key = name + ':' + JSON.stringify(args);
    if (seen.has(key)) continue;
    seen.add(key);
    calls.push({ name, args, raw: body.trim() });
  }

  // 3b) 兜底：标记被截断（缺少闭合标签）时，尽量把工具名与参数捞出来
  if (!calls.length && NATIVE_MARK && new RegExp(NATIVE_MARK, 'i').test(raw)) {
    const broken = /<invoke\s+name\s*=\s*["']([^"']+)["']\s*>([\s\S]*)$/i.exec(norm);
    if (broken && TOOLS[broken[1].trim()]) {
      const name = broken[1].trim();
      const args = {};
      const pre = /<parameter\s+name\s*=\s*["']([^"']+)["']\s*>([\s\S]*?)(?=<parameter|$)/gi;
      let q;
      while ((q = pre.exec(broken[2]))) {
        args[q[1].trim()] = decodeEntities(q[2]).replace(/<\/?[^>]*>/g, '').trim();
      }
      calls.push({ name, args, raw: broken[2].trim() });
    }
  }
  return calls;
}

function stripToolBlocks(text) {
  return normalizeModelMarkup(text)
    .replace(/<tool\s+name\s*=\s*["'][^"']+["']\s*>[\s\S]*?<\/tool>/gi, '')
    .replace(/<invoke\s+name\s*=\s*["'][^"']+["']\s*>[\s\S]*?<\/invoke>/gi, '')
    .replace(/<parameter\s+name\s*=\s*["'][^"']+["']\s*>[\s\S]*?<\/parameter>/gi, '')
    .replace(/<\/?(?:invoke|parameter)\b[^>]*>/gi, '')
    .replace(/```(?:tool)[:\s]*[a-z_]+\s*\n[\s\S]*?```/gi, '')
    .replace(/\/?\s*[|｜丨]{1,4}\s*(?:D\s*S\s*M\s*L)\s*[|｜丨]{1,4}\s*[a-zA-Z_\/]*\s*/gi, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

async function executeTool(room, agent, name, args) {
  const tool = TOOLS[name];
  if (!tool) return { ok: false, result: `未知工具：${name}` };
  if (tool.write && agent.canModifyFiles === false) {
    return { ok: false, result: `你没有修改文件的权限（${name} 被拒绝）。只有发言权，请把修改请求交给有权限的人。` };
  }
  try { return { ok: true, result: String(await tool.run(room, args || {})) }; }
  catch (e) { return { ok: false, result: `执行失败：${e.message}` }; }
}

/* ================================================================== */
/* 模型调用                                                           */
/* ================================================================== */

async function callLLM(agent, messages) {
  const apiBase = resolveSecret(agent.apiBase).replace(/\/+$/, '');
  const apiKey = resolveSecret(agent.apiKey);
  if (!apiBase) throw new Error('未配置 apiBase');
  const url = /\/chat\/completions$/.test(apiBase) ? apiBase : `${apiBase}/chat/completions`;

  const controller = new AbortController();
  state.runner.abort = controller;
  const timer = setTimeout(() => controller.abort(), 180000);
  const t0 = Date.now();
  try {
    const res = await fetch(url, {
      method: 'POST',
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) },
      body: JSON.stringify({
        model: agent.model,
        messages,
        temperature: typeof agent.temperature === 'number' ? agent.temperature : 0.7,
        stream: false,
      }),
    });
    const bodyText = await res.text();
    if (!res.ok) throw new Error(`模型接口返回 ${res.status}：${truncate(bodyText, 400)}`);
    let data;
    try { data = JSON.parse(bodyText); }
    catch { throw new Error('模型接口返回的不是合法 JSON：' + truncate(bodyText, 300)); }
    const choice = data.choices && data.choices[0];
    const msg = (choice && choice.message) || {};
    let content = String(msg.content ?? msg.reasoning_content ?? '');

    // 兜底：有些兼容接口会把函数调用放在 message.tool_calls 里返回，
    // 这种情况 content 可能是空的。把它翻译成本项目认识的格式，交给统一解析。
    if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
      const rendered = msg.tool_calls.map((tc) => {
        const name = (tc.function && tc.function.name) || tc.name || '';
        let args = (tc.function && tc.function.arguments) || tc.arguments || '{}';
        if (typeof args === 'string') {
          try { args = JSON.parse(args); } catch { args = {}; }
        }
        return `<tool name="${name}">\n${JSON.stringify(args)}\n</tool>`;
      }).join('\n');
      content = content ? content + '\n\n' + rendered : rendered;
    }

    return { content, usage: data.usage || {}, ms: Date.now() - t0 };
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('请求已中止（用户停止或超时）');
    throw e;
  } finally {
    clearTimeout(timer);
    if (state.runner.abort === controller) state.runner.abort = null;
  }
}

/* ================================================================== */
/* 组装上下文                                                         */
/* ================================================================== */

async function buildMessages(room, agent) {
  const others = room.agents.filter((a) => a.enabled && a.id !== agent.id);
  const files = await listFiles(room);

  const system = [
    worldbox.rolePromptFor(agent) || agent.systemPrompt || '',
    '',
    '## 房间信息',
    `- 房间名：${room.name}`,
    `- 房间设定：${room.setting || '（未设置）'}`,
    `- 你是：${agent.name}`,
    `- 房间里的其他人：${others.length ? others.map((a) => a.name).join('、') : '（暂时只有你）'}`,
    `- 用户（人类）也在房间里，署名"用户"。`,
    '',
    `## 本房间工作区（目录 room/${room.dir}/）`,
    files.length ? files.map((f) => `- ${f.path} (${f.size} bytes)`).join('\n') : '（空）',
    '',
    toolProtocolFor(agent, room),
    '',
    // WorldBox 世界局势（只在世界房间注入；普通房间返回空串）
    ...(worldbox.bridgeEnabled(room)
      ? [await worldbox.systemSection(room, agent, worldbox.getBridge(room)), '']
      : []),
    // 创世者对上一轮提案的裁决（回填闭环）
    ...(worldbox.bridgeEnabled(room)
      ? (worldboxFeedback(room) ? [worldboxFeedback(room), ''] : [])
      : []),
    // 决策额度：一次决策是有限的，必须轻度、小步
    ...(worldbox.bridgeEnabled(room) ? [worldboxBudget(room, agent), ''] : []),
    '## 发言准则',
    '- 用中文，像在群里说话一样自然，不要复读别人说过的话。',
    '- 保持你的人设和立场；你有自己的目的和判断，不需要讨好所有人。',
    '- 每次发言都要推进事情：给结论、给产出、或给明确的下一步。',
    '- 需要别人接话时用 @名字 点名。',
    '- 不要写"作为AI"之类的客套话，直接说话。',
  ].join('\n');

  const history = room.messages.filter((m) => m.role !== 'system').slice(-(room.historyLimit || 60));

  const out = [{ role: 'system', content: system }];
  for (const m of history) {
    let text = m.text || '';
    if (m.toolCount) text += `\n（这一轮你操作了工作区，共 ${m.toolCount} 次工具调用）`;
    if (!text.trim()) continue;
    const isSelf = m.role === 'agent' && m.agentId === agent.id;
    const role = isSelf ? 'assistant' : 'user';
    const content = isSelf ? text : `【${m.name}】${text}`;
    const last = out[out.length - 1];
    if (last && last.role === role) last.content += '\n\n' + content;
    else out.push({ role, content });
  }
  if (out[out.length - 1] && out[out.length - 1].role === 'assistant') {
    out.push({ role: 'user', content: '（继续，或者把话头 @ 给合适的其他人。）' });
  }
  if (out.length === 1) {
    out.push({ role: 'user', content: '（房间里还没有人说话，请你先开口，进入你的角色。）' });
  }
  return out;
}

/* ================================================================== */
/* 消息                                                               */
/* ================================================================== */

function pushMessage(room, msg) {
  const full = { id: uid('m'), ts: Date.now(), ...msg };
  room.messages.push(full);
  if (room.messages.length > MAX_ROOM_MSGS) room.messages.splice(0, room.messages.length - MAX_ROOM_MSGS);
  broadcast('message', { roomId: room.id, message: full });
  scheduleSave();
  return full;
}

function pushSystem(room, text) {
  return pushMessage(room, { role: 'system', name: '系统', text });
}

function findMentioned(room, text) {
  const found = [];
  if (!text) return found;
  for (const a of room.agents) {
    if (!a.enabled) continue;
    const re = new RegExp('@' + a.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?![\\w\\u4e00-\\u9fa5])');
    if (re.test(text)) found.push(a.id);
  }
  return found;
}

/* ================================================================== */
/* WorldBox：国策解析 / 动作账本 / 世界节拍                           */
/* ================================================================== */

// 落点型：执行：请创世者在 北平(4,23) 增建/升级住房，容量 +83
const SITE_RE = /执行[^\n:：]*[:：][^\n]*?([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{1,20}?)\s*[（(]\s*(\d{1,4})\s*[,，]\s*(\d{1,4})\s*[)）]([^\n]*)/;
// 开关型：执行（全国开关）：把「地方高税」下调一档
const SWITCH_RE = /执行\s*[（(]\s*全国开关\s*[)）]\s*[:：]\s*([^\n]+)/;

function parsePolicyActions(text, nation) {
  const out = [];
  for (const line of String(text || '').split('\n')) {
    const s = line.trim();
    if (!s.includes('执行')) continue;
    let m = SWITCH_RE.exec(s);
    if (m) { out.push({ nation, kind: '开关型', target: null, coord: null, summary: m[1].trim() }); continue; }
    m = SITE_RE.exec(s);
    if (m) {
      out.push({
        nation, kind: '落点型', target: m[1].trim(),
        coord: [Number(m[2]), Number(m[3])], summary: m[1].trim() + m[4].trim(),
      });
    }
  }
  return out;
}

/** 一轮决策的额度说明：数量上限 + 轻度原则。 */
function worldboxBudget(room, agent) {
  const cap = Math.max(1, Number(room.worldbox && room.worldbox.maxActionsPerTurn) || 3);
  const nation = worldbox.resolveNationFor(room, agent) || agent.name;
  const pending = (room.actions || []).filter(
    (a) => (a.status === 'proposed' || a.status === 'unparsed') && a.nation === nation
  ).length;
  return [
    `## 决策额度（硬性限制，本轮最多 ${cap} 条）`,
    '',
    `- 你这一轮**最多只能提出 ${cap} 条**可执行动作（要创世者动手的那种）。`,
    `  写出第 ${cap + 1} 条及以后的会被系统直接丢弃，等于白说。`,
    '- **一次决策是有限的：必须轻度。** 每一条都应当是小步、具体、可逆的调整，',
    '  而不是"重建整个国家"。宁可只提 1 条做得成的，也不要凑满额度。',
    '- 优先级：先救命（饥饿、无家可归、人口流失），再补短板，最后才轮到扩张与战争。',
    '- 想不清楚就先只提 1 条；只想表态、谈判、结盟、放狠话，那就**一条都不提**，直接说话。',
    '- 额度只限制"要创世者动手"的条目。**说话、谈判、结盟、回击不消耗额度。**',
    '- 已经提出、还在等创世者裁决的动作不要重复提交。',
    pending ? `- 你当前有 ${pending} 条提案还在等裁决。` : '',
  ].filter(Boolean).join('\n');
}

/** 把某个角色发言里的可执行动作记进本房间的账本。 */
function recordPolicyActions(room, agent, text) {
  if (!worldbox.bridgeEnabled(room)) return { added: [], dropped: 0, parsed: 0 };
  const cap = Math.max(1, Number(room.worldbox.maxActionsPerTurn) || 3);
  const nation = worldbox.resolveNationFor(room, agent) || agent.name;
  const fp = room.worldbox.lastFingerprint || null;
  const parsed = parsePolicyActions(text, nation);

  if (!parsed.length) {
    // 提到"执行"或写了《国策》却解析不出动作 → 保留原文，交给玩家判断
    if (/国策|政策|执行/.test(text) && text.length > 40) {
      room.actions.push({
        id: uid('act'), nation, agentId: agent.id, agentName: agent.name, kind: '未解析',
        target: null, coord: null, summary: truncate(text.replace(/\s+/g, ' '), 160),
        raw: truncate(text, 4000), status: 'unparsed', fingerprint: fp,
        createdAt: Date.now(), decidedAt: null, note: '未能解析出可执行动作，请让它按「执行：请创世者在 城市(x,y) …」重写',
      });
      broadcast('actions', { roomId: room.id, actions: room.actions.slice(-200) });
      scheduleSave();
    }
    return { added: [], dropped: 0, parsed: 0 };
  }

  // 先去掉重复的（同一帧 + 同一国家 + 同一句话，已入账过）
  const fresh = parsed.filter((a) => !room.actions.some(
    (x) => x.fingerprint === fp && x.nation === a.nation && x.summary === a.summary
      && (x.status === 'proposed' || x.status === 'executed')
  ));

  // 额度：一轮最多 cap 条，超出的直接丢弃
  const keep = fresh.slice(0, cap);
  const dropped = fresh.slice(cap);

  const added = [];
  for (const a of keep) {
    const item = {
      id: uid('act'), ...a, agentId: agent.id, agentName: agent.name,
      status: 'proposed', fingerprint: fp, createdAt: Date.now(), decidedAt: null, note: '',
    };
    room.actions.push(item);
    added.push(item);
  }
  if (room.actions.length > 400) room.actions.splice(0, room.actions.length - 400);
  if (added.length) {
    broadcast('actions', { roomId: room.id, actions: room.actions.slice(-200) });
    scheduleSave();
  }
  if (dropped.length) {
    log(`${room.name} / ${agent.name} 超出额度，丢弃 ${dropped.length} 条动作`);
  }
  return { added, dropped: dropped.length, parsed: parsed.length };
}

/** 世界推进后，上一帧还没被裁决的提案作废。 */
function expireStaleActions(room, fingerprint) {
  let n = 0;
  for (const a of room.actions) {
    if ((a.status === 'proposed' || a.status === 'unparsed') && a.fingerprint && a.fingerprint !== fingerprint) {
      a.status = 'obsolete';
      a.decidedAt = Date.now();
      a.note = '世界已推进，该提案针对的局势已过去';
      n++;
    }
  }
  if (n) broadcast('actions', { roomId: room.id, actions: room.actions.slice(-200) });
  return n;
}

/** 注入给角色的"创世者裁决回执"。 */
function worldboxFeedback(room) {
  if (!room.actions || !room.actions.length) return '';
  const decided = room.actions.filter((a) => ['executed', 'rejected', 'obsolete'].includes(a.status)).slice(-8);
  if (!decided.length) return '';
  const label = { executed: '✅ 已执行', rejected: '❌ 被拒绝', obsolete: '⌛ 已过期（世界已推进）' };
  return [
    '## 创世者裁决回执（你过去提出的政策的下场）',
    '',
    ...decided.map((a) => `- [${a.nation}] ${a.summary} → ${label[a.status]}${a.note ? `（${a.note}）` : ''}`),
    '',
    '被拒绝或已过期的提案不要重复提；如果局势仍然需要处理，请基于**最新**情报重新拟一份。',
  ].join('\n');
}

/** 读一次世界状态：更新指纹、作废过时提案、广播。 */
async function syncWorld(room, { postNotice = false } = {}) {
  if (!worldbox.bridgeEnabled(room)) return null;
  const bridge = worldbox.getBridge(room);
  const st = await bridge.status();
  if (!st) { broadcast('world', { roomId: room.id, status: null }); return null; }
  const prev = room.worldbox.lastFingerprint;
  if (prev && prev !== st.fingerprint) {
    const n = expireStaleActions(room, st.fingerprint);
    room.worldbox.lastFingerprint = st.fingerprint;
    if (postNotice) {
      pushSystem(room, `🌍 世界推进：${st.worldName} 第 ${st.year} 年。` +
        (n ? `上一帧有 ${n} 条提案因局势已变作废。` : '局势已更新，请基于新情报决策。'));
    }
    scheduleSave();
  } else if (!prev) {
    room.worldbox.lastFingerprint = st.fingerprint;
    scheduleSave();
  }
  broadcast('world', { roomId: room.id, status: st });
  return st;
}

/**
 * 世界数据变了以后，让领袖的人设跟上。
 *
 * 为什么需要：房间创建时生成的人设只写了"你是 X 国的领袖"，没有客观国情。
 * 而角色在做决策时看到的世界局势是**每轮动态注入**的，人设却可能停留在
 * 几小时前——甚至停留在另一个世界（使用者换了存档之后）。
 *
 * 这里做的事：把每个角色所属国家的**客观事实**（国王与其特质、国家特质、
 * 国力地位、人口与军队趋势）写进 systemPromptAuto，并在世界变化后重算。
 *
 * 两条纪律：
 *   · **绝不覆盖使用者手写的 systemPrompt** —— 那是他对角色的设定，优先级最高；
 *   · **只在空白或世界已变时生成** —— 避免每轮都重写、把上下文搅乱。
 */
async function syncWorldboxPrompts(room, { force = false, postNotice = false } = {}) {
  if (!worldbox.bridgeEnabled(room)) return { filled: 0, refreshed: 0, skipped: 0 };
  const bridge = worldbox.getBridge(room);
  const res = await worldbox.autofillSystemPrompts(room, bridge, { force });
  if (res.filled || res.refreshed) {
    scheduleSave();
    if (postNotice) {
      const parts = [];
      if (res.filled) parts.push(`为 ${res.filled} 位领袖生成了人设`);
      if (res.refreshed) parts.push(`因世界变化重算了 ${res.refreshed} 位`);
      pushSystem(room, `👑 ${parts.join('，')}。`);
    }
    broadcast('agents', publicRoom(room));
  }
  return res;
}

/**
 * 给网页端的 WorldBox 面板数据。
 *
 * @param {object} room
 * @param {object} [opts]
 * @param {boolean} [opts.withSaves=true]  是否附带完整存档列表。
 *   `/api/state` 每回合都会被前端拉取，而存档可能有几十份，把整份列表塞进
 *   每次响应既慢又没意义——那里只要当前锁定值与数量即可。
 */
async function worldboxInfo(room, opts = {}) {
  if (!room || !worldbox.bridgeEnabled(room)) return { enabled: false };
  const withSaves = opts.withSaves !== false;
  const bridge = worldbox.getBridge(room);
  const [st, line, nations, savesInfo] = await Promise.all([
    bridge.status(),
    bridge.statusLine(),
    bridge.nations(),
    bridge.saves(withSaves ? 0 : 1),      // limit=1 时只取数量，不传整份列表
  ]);
  const proposed = room.actions.filter((a) => a.status === 'proposed' || a.status === 'unparsed').length;
  const info = {
    enabled: true, kind: room.kind, baseUrl: room.worldbox.baseUrl,
    strictTick: room.worldbox.strictTick, nationOf: room.worldbox.nationOf,
    maxActionsPerTurn: room.worldbox.maxActionsPerTurn,
    lastFingerprint: room.worldbox.lastFingerprint,
    // 当前锁定的存档。null = 跟随最新。
    saveKey: room.worldbox.saveKey || null,
    available: !!st, line, status: st, proposed,
    nations: (nations || []).map((n) => ({
      id: n.id, name: n['名称'], live: n['是否存续'], renown: n['声望'],
      army: n['军队兵力'], population: n['人口'], cities: n['城市数'], ruler: n['国王'],
    })),
    savesCount: savesInfo.count || 0,
    saveSelection: savesInfo.selection || null,
    dataDir: savesInfo.dataDir || null,
  };
  if (withSaves) {
    // 存档选择：一个正常玩家有几十个存档、分属不同世界，必须能挑。
    info.saves = (savesInfo.saves || []).map((r) => ({
      key: r.key, index: r.index, kind: r.kind_label, world: r.world_name,
      time: r.time_local, age: r.age_text, stale: r.stale,
      hasStats: r.has_stats, sizeMb: r.size_mb, selected: !!r.selected,
    }));
  }
  return info;
}

/** 把 skills/ 复制进世界房间的工作区，让领袖能读到扮演规范。 */
async function copyWorldboxSkills(room) {
  try {
    await fsp.cp(path.join(ROOT, 'skills'), roomPath(room, 'skills'), { recursive: true });
    return true;
  } catch { return false; }
}

/* ================================================================== */
/* 发言：一整轮（含工具循环）                                         */
/* ================================================================== */

async function runAgentTurn(room, agent) {
  broadcast('inner-status', { roomId: room.id, agentId: agent.id, phase: 'thinking' });
  const messages = await buildMessages(room, agent);

  const toolCalls = [];
  let finalText = '';
  let usageTotal = { prompt_tokens: 0, completion_tokens: 0 };
  const maxSteps = Math.max(1, Number(room.maxToolSteps) || 4);

  for (let step = 0; step < maxSteps; step++) {
    if (state.runner.stopRequested) break;
    const { content, usage, ms } = await callLLM(agent, messages);
    usageTotal.prompt_tokens += usage.prompt_tokens || 0;
    usageTotal.completion_tokens += usage.completion_tokens || 0;
    if (step === 0) bumpStat(room, agent.id, { turns: 1 });

    const calls = parseToolCalls(content);
    if (!calls.length) {
      finalText = content.trim();
      pushInner(room, agent, 'think', { text: content, meta: { ms, step: step + 1, final: true } });
      break;
    }

    // 需要动手 → 这段内容属于「内心」
    pushInner(room, agent, 'think', {
      text: stripToolBlocks(content) || '（决定调用工具）',
      meta: { ms, step: step + 1, toolCount: calls.length },
    });
    messages.push({ role: 'assistant', content });

    const results = [];
    for (const call of calls) {
      let outcome;
      if (call.parseError) outcome = { ok: false, result: `参数不是合法 JSON：${truncate(call.raw, 200)}` };
      else outcome = await executeTool(room, agent, call.name, call.args);
      const rec = { name: call.name, args: call.args || {}, ok: outcome.ok, result: truncate(outcome.result, 6000), ts: Date.now() };
      toolCalls.push(rec);
      results.push(`<tool_result name="${call.name}" ok="${outcome.ok}">\n${rec.result}\n</tool_result>`);
      pushInner(room, agent, 'tool', { tool: rec, text: `${call.name}${rec.args.path ? ' → ' + rec.args.path : ''}` });
      broadcast('tool-live', { roomId: room.id, agentId: agent.id, agentName: agent.name, call: rec });
      log(`${room.name} / ${agent.name} → ${call.name} ${outcome.ok ? 'ok' : 'fail'}`);
    }
    messages.push({
      role: 'user',
      content: `工具执行结果：\n\n${results.join('\n\n')}\n\n请继续：要么调用更多工具，要么给出你的最终发言。`,
    });
    if (step === maxSteps - 1) finalText = stripToolBlocks(content) || '（工具步骤已达上限，未给出总结）';
  }

  if (!finalText && !toolCalls.length) finalText = '（本回合没有输出）';

  bumpStat(room, agent.id, {
    promptTokens: usageTotal.prompt_tokens,
    completionTokens: usageTotal.completion_tokens,
  });

  const msg = pushMessage(room, {
    role: 'agent', agentId: agent.id, name: agent.name, emoji: agent.emoji, color: agent.color,
    model: agent.model, text: finalText, toolCount: toolCalls.length,
    tokens: usageTotal.prompt_tokens + usageTotal.completion_tokens,
  });
  pushInner(room, agent, 'speak', { text: finalText, meta: { messageId: msg.id, toolCount: toolCalls.length } });

  // WorldBox 房间：从发言里抽出可执行动作，进"待执行清单"
  if (worldbox.bridgeEnabled(room)) {
    const cap = Math.max(1, Number(room.worldbox.maxActionsPerTurn) || 3);
    const { added, dropped, parsed } = recordPolicyActions(room, agent, finalText);
    if (added.length || dropped) {
      const lines = [`本轮额度 ${cap} 条，解析出 ${parsed} 条，入账 ${added.length} 条：`]
        .concat(added.map((a) => `· [${a.kind}] ${a.summary}`));
      if (dropped) {
        lines.push(`⚠️ 有 ${dropped} 条超出本轮额度，已被丢弃（决策要轻度、小步，一次别贪多）。`);
      }
      pushInner(room, agent, 'policy', {
        text: lines.join('\n'),
        meta: { actions: added.map((a) => ({ id: a.id, kind: a.kind, nation: a.nation, coord: a.coord })), dropped, cap },
      });
      log(`${room.name} / ${agent.name} 国策动作：入账 ${added.length}/${parsed}（额度 ${cap}，丢弃 ${dropped}）`);
      if (dropped) {
        pushSystem(room, `⚠️ ${agent.name} 本轮提出了 ${parsed} 条动作，超出额度（上限 ${cap} 条），` +
          `只有前 ${cap} 条进入待执行清单。`);
      }
    }
  }

  if (room.followMentions && !state.runner.stopRequested && room.mode !== 'on_demand') {
    for (const id of findMentioned(room, finalText)) {
      if (room.queue.length + 1 > (Number(room.maxTurns) || 40)) break;
      if (!room.queue.includes(id)) room.queue.push(id);
    }
    broadcast('queue', { roomId: room.id, queue: room.queue.slice() });
  }
  broadcast('inner-status', { roomId: room.id, agentId: agent.id, phase: 'done' });
  return msg;
}

function bumpStat(room, agentId, patch) {
  const s = (room.stats[agentId] ||= { turns: 0, promptTokens: 0, completionTokens: 0, errors: 0 });
  for (const [k, v] of Object.entries(patch)) s[k] = (s[k] || 0) + v;
}

/* ================================================================== */
/* 按需发言：让 Agent 自己判断要不要说话                              */
/* ================================================================== */

function parseDecision(text) {
  const t = String(text || '').trim().replace(/^[*#>\s]+/, '');
  const first = (t.split('\n')[0] || '').trim();
  const en = first.match(/^(YES|NO)\b/i);
  if (en) return { yes: en[1].toUpperCase() === 'YES', reason: first.slice(en[0].length).replace(/^[\s:：,，-]+/, '').trim() };
  if (/^(不需要|不必|无需|不用|不发言|否|没有|无)/.test(first)) return { yes: false, reason: first };
  if (/^(需要|要|是|该我|我来|发言)/.test(first)) return { yes: true, reason: first };
  if (/yes/i.test(first) && !/no/i.test(first)) return { yes: true, reason: first };
  return { yes: false, reason: first || '（无法解析，视为不发言）' };
}

async function pollDecision(room, agent, excludeId) {
  const recent = room.messages.filter((m) => m.role !== 'system').slice(-12);
  const transcript = recent
    .map((m) => `${m.role === 'user' ? '用户' : m.name}：${truncate(m.text, 400)}`)
    .join('\n');
  const last = recent[recent.length - 1];

  const sys = [
    // 判断"要不要发言"时也该带上完整人设，否则领袖在这一步没有性格。
    // 世界局势刻意不注入——这一步只需要轻量判断，不需要几 KB 的战报。
    worldbox.rolePromptFor(agent) || agent.systemPrompt || '',
    '',
    `房间设定：${room.setting || '（未设置）'}`,
    `你是「${agent.name}」。现在不是让你长篇发言，而是让你快速判断：基于刚才的对话，你要不要开口。`,
  ].join('\n');

  const usr = [
    '最近的对话：',
    transcript || '（还没人说话）',
    '',
    `最后一句是 ${last ? (last.role === 'user' ? '用户' : last.name) : '空'} 说的。`,
    '',
    '请判断：你现在需要发言吗？',
    '只输出一行，格式为「YES 理由」或「NO 理由」，理由不超过 20 字。',
    '需要发言的情形：有人 @ 你；有你必须回应的质问或点名；你有别人都没提到的关键信息；该你推进下一步。',
    '不需要发言的情形：话题与你无关；你想说的已经被说过了；插话只会重复；你对现状没有补充。',
    excludeId ? '' : '',
  ].filter(Boolean).join('\n');

  const t0 = Date.now();
  const { content, usage } = await callLLM(agent, [
    { role: 'system', content: sys },
    { role: 'user', content: usr },
  ]);
  const d = parseDecision(content);
  pushInner(room, agent, 'decide', {
    text: `${d.yes ? '决定发言' : '决定不发言'}：${d.reason}`,
    meta: { yes: d.yes, raw: truncate(content.trim(), 300), ms: Date.now() - t0 },
  });
  bumpStat(room, agent.id, {
    promptTokens: usage.prompt_tokens || 0,
    completionTokens: usage.completion_tokens || 0,
  });
  return { agentId: agent.id, yes: d.yes, reason: d.reason };
}

/* ================================================================== */
/* 调度循环（三种模式统一）                                           */
/* ================================================================== */

async function runLoop(room, opts = {}) {
  if (state.runner.running) return;

  // WorldBox 节拍：世界指纹没变就不要重复决策（避免在同一帧数据上刷同样的国策）
  if (worldbox.bridgeEnabled(room) && room.worldbox.strictTick && !opts.force) {
    const prev = room.worldbox.lastFingerprint;
    const st = await syncWorld(room);
    if (st && prev && prev === st.fingerprint) {
      pushSystem(room, '（世界尚未推进 —— 局势没有变化。可以继续谈判、结盟与表态，' +
        '但先不要产出新的《国策》。点「强制开一轮」可以忽略这个限制。）');
      room.queue = [];
      broadcast('queue', { roomId: room.id, queue: [] });
      return;
    }
  } else if (worldbox.bridgeEnabled(room)) {
    await syncWorld(room, { postNotice: true });
  }

  state.runner.running = true;
  state.runner.roomId = room.id;
  state.runner.stopRequested = false;
  broadcast('run', { roomId: room.id, running: true, currentAgentId: null, runningRoomId: room.id });

  const maxTurns = Number(room.maxTurns) || 40;
  let turns = 0;
  let consecutiveErrors = 0;
  let declines = 0;
  const onDemand = room.mode === 'on_demand';

  try {
    while (!state.runner.stopRequested && turns < maxTurns) {
      let agentId = null;

      if (room.queue.length) {
        agentId = room.queue.shift();
        broadcast('queue', { roomId: room.id, queue: room.queue.slice() });
      } else if (onDemand) {
        const lastSpeaker = [...room.messages].reverse().find((m) => m.role === 'agent' || m.role === 'user');
        const candidates = room.agents.filter((a) => a.enabled && (!lastSpeaker || a.id !== lastSpeaker.agentId));
        if (!candidates.length) break;
        broadcast('deciding', { roomId: room.id, agentIds: candidates.map((a) => a.id) });
        pushInnerAll(room, candidates, 'decide', '正在判断是否需要发言…', { pending: true });
        const results = await Promise.all(
          candidates.map((a) =>
            pollDecision(room, a, lastSpeaker && lastSpeaker.agentId).catch((e) => {
              bumpStat(room, a.id, { errors: 1 });
              pushInner(room, a, 'error', { text: `判断失败：${e.message}` });
              return { agentId: a.id, yes: false, reason: '判断失败' };
            })
          )
        );
        const yes = results.filter((r) => r.yes).map((r) => r.agentId);
        log(`${room.name} 判断结果：${results.map((r) => (r.yes ? 'YES' : 'no')).join('/')}`);
        if (!yes.length) {
          declines++;
          if (declines >= 2) {
            pushSystem(room, '没有人需要继续发言，讨论告一段落。');
            break;
          }
          continue;
        }
        declines = 0;
        room.queue.push(...yes.slice(0, 4));
        broadcast('queue', { roomId: room.id, queue: room.queue.slice() });
        continue;
      } else {
        break; // round_robin / manual：队列空了就结束
      }

      if (!agentId) continue;
      const agent = room.agents.find((a) => a.id === agentId);
      if (!agent || !agent.enabled) continue;

      room.currentAgentId = agent.id;
      broadcast('run', { roomId: room.id, running: true, currentAgentId: agent.id, runningRoomId: room.id });
      try {
        await runAgentTurn(room, agent);
        consecutiveErrors = 0;
      } catch (e) {
        consecutiveErrors++;
        bumpStat(room, agent.id, { errors: 1 });
        pushInner(room, agent, 'error', { text: String(e.message || e) });
        pushSystem(room, `⚠️ ${agent.name} 调用失败：${e.message}`);
        log('turn error:', e.message);
      }
      turns++;
      room.currentAgentId = null;
      broadcast('run', { roomId: room.id, running: true, currentAgentId: null, runningRoomId: room.id });

      if (consecutiveErrors >= 3) {
        pushSystem(room, '连续 3 次调用失败，已自动停止。请检查各角色的 API 配置。');
        break;
      }
      const delay = Number(agent.turnDelayMs ?? 350);
      if (delay > 0 && (room.queue.length || onDemand) && !state.runner.stopRequested) await sleep(delay);
    }
    if (turns >= maxTurns) pushSystem(room, `已达到最大回合数限制（${maxTurns}），停止。`);
  } finally {
    state.runner.running = false;
    state.runner.roomId = null;
    state.runner.abort = null;
    room.currentAgentId = null;
    room.queue = [];
    broadcast('run', { roomId: room.id, running: false, currentAgentId: null, runningRoomId: null });
    broadcast('queue', { roomId: room.id, queue: [] });
    scheduleSave();
  }
}

function pushInnerAll(room, agents, kind, text, meta) {
  for (const a of agents) pushInner(room, a, kind, { text, meta });
}

function buildQueue(room, { mode, rounds, agentIds }) {
  const enabled = room.agents.filter((a) => a.enabled && (!agentIds || agentIds.includes(a.id)));
  if (mode === 'manual') return [];
  if (mode === 'on_demand') return []; // 由判断循环自己排队
  const n = Math.max(1, Number(rounds) || 1);
  const q = [];
  for (let r = 0; r < n; r++) for (const a of enabled) q.push(a.id);
  return q;
}

/* ================================================================== */
/* HTTP 工具                                                          */
/* ================================================================== */

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
};

function sendJSON(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) { reject(new Error('请求体过大')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch { reject(new Error('请求体不是合法 JSON')); }
    });
    req.on('error', reject);
  });
}

async function serveStatic(res, urlPath) {
  let rel = decodeURIComponent(urlPath.split('?')[0]);
  if (rel === '/' || rel === '') rel = '/index.html';
  const abs = path.resolve(PUBLIC_DIR, '.' + rel);
  if (path.relative(PUBLIC_DIR, abs).startsWith('..')) return sendJSON(res, 403, { error: '禁止访问' });
  try {
    const data = await fsp.readFile(abs);
    res.writeHead(200, { 'Content-Type': MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream' });
    res.end(data);
  } catch {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('404 Not Found');
  }
}

/* ================================================================== */
/* 路由                                                               */
/* ================================================================== */

async function handleApi(req, res, url) {
  const p = url.pathname;

  if (req.method === 'GET' && p === '/api/state') {
    const room = activeRoom();
    return sendJSON(res, 200, {
      ...publicGlobal(),
      active: room
        ? {
            ...publicRoomFull(room),
            files: await listFiles(room),
            actions: room.actions.slice(-200),
            // withSaves:false —— 这个端点每次发言/轮询都会被拉，
            // 不该把几十份存档的完整列表塞进每一次响应。
            // 需要列表请用 GET /api/worldbox/saves。
            worldbox: await worldboxInfo(room, { withSaves: false }),
          }
        : null,
    });
  }

  if (req.method === 'GET' && p === '/api/worldbox') {
    const room = activeRoom();
    if (!room) return sendJSON(res, 200, { enabled: false });
    return sendJSON(res, 200, {
      ...(await worldboxInfo(room)),
      actions: room.actions.slice(-200),
    });
  }

  if (req.method === 'GET' && p === '/api/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform', Connection: 'keep-alive', 'X-Accel-Buffering': 'no',
    });
    res.write('retry: 3000\n\n');
    res.write(`event: hello\ndata: ${JSON.stringify({ ts: Date.now() })}\n\n`);
    sseClients.add(res);
    const ping = setInterval(() => { try { res.write(': ping\n\n'); } catch { /* */ } }, 15000);
    req.on('close', () => { clearInterval(ping); sseClients.delete(res); });
    return;
  }

  const room = activeRoom();
  const needRoom = () => {
    if (!room) { sendJSON(res, 400, { error: '当前没有激活的房间' }); return false; }
    return true;
  };

  if (req.method === 'GET' && p === '/api/files') {
    if (!needRoom()) return;
    return sendJSON(res, 200, { files: await listFiles(room) });
  }

  if (req.method === 'GET' && p === '/api/file') {
    if (!needRoom()) return;
    try {
      const { abs, rel } = safePath(room, url.searchParams.get('path'));
      return sendJSON(res, 200, { path: rel, content: await fsp.readFile(abs, 'utf8') });
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  const body = ['POST', 'PUT', 'DELETE'].includes(req.method) ? await readBody(req) : {};

  /* ---------------- 房间管理 ---------------- */

  if (req.method === 'POST' && p === '/api/rooms/create') {
    const kind = body.kind === 'worldbox' ? 'worldbox' : 'normal';
    const name = String(body.name || '').trim()
      || (kind === 'worldbox' ? 'WorldBox 世界频道' : `房间 ${state.rooms.length + 1}`);
    const dir = nextRoomDir(kind);

    let agents;
    let setting;
    let worldboxCfg = null;
    let worldSummary = '';

    if (kind === 'worldbox') {
      setting = WORLDBOX_SETTING;
      const baseUrl = String(body.baseUrl || worldbox.DEFAULT_BASE);
      const probe = new worldbox.WorldBridge({ baseUrl, timeoutMs: 5000 });
      let nations = [];
      try { nations = await probe.nations(); } catch { nations = []; }
      probe.dispose();
      if (nations.length) {
        agents = nations.map((n, i) => leaderAgent(n, i));
        worldboxCfg = {
          enabled: true, baseUrl, strictTick: true,
          nationOf: Object.fromEntries(nations.map((n, i) => [agents[i].name, n['名称']])),
        };
        worldSummary = `已按世界数据建立 ${nations.length} 位国家领袖：${agents.map((a) => a.name).join('、')}`;
      } else {
        agents = placeholderLeaders();
        worldboxCfg = { enabled: true, baseUrl, strictTick: true, nationOf: {} };
        worldSummary = '暂时连不上世界数据服务，先建了 3 个占位领袖，稍后在右侧面板分配国家';
      }
    } else {
      setting = '这是一个新房间。请描述场景、有哪些角色、他们之间是什么关系、当前正在发生什么。';
      agents = defaultAgents();
    }

    const r = normalizeRoom({ kind, name, dir, setting, agents, worldbox: worldboxCfg });
    state.rooms.push(r);
    await fsp.mkdir(roomPath(r), { recursive: true });
    if (kind === 'worldbox') await copyWorldboxSkills(r);
    await syncWorld(r);
    // 房间刚建好时人也只是"你是 X 国的领袖"，缺客观国情。
    // 这里按真实数据补齐（国王、国家特质、国力地位、人口/军队趋势），
    // 让领袖一开口就有立场；使用者之后手写的人设不会被覆盖。
    if (kind === 'worldbox') {
      const filled = await syncWorldboxPrompts(r, { force: true });
      if (filled.filled) worldSummary += `；已按世界数据为 ${filled.filled} 位领袖生成人设`;
    }
    state.activeRoomId = r.id;
    broadcast('rooms', publicGlobal());
    scheduleSave();
    return sendJSON(res, 200, { ok: true, room: publicRoom(r), note: worldSummary });
  }

  if (req.method === 'POST' && p === '/api/rooms/activate') {
    const r = roomById(body.id);
    if (!r) return sendJSON(res, 404, { error: '房间不存在' });
    state.activeRoomId = r.id;
    broadcast('rooms', publicGlobal());
    scheduleSave();
    return sendJSON(res, 200, { ok: true, activeRoomId: r.id });
  }

  if (req.method === 'POST' && p === '/api/rooms/update') {
    const r = roomById(body.id || state.activeRoomId);
    if (!r) return sendJSON(res, 404, { error: '房间不存在' });
    if (body.name != null) r.name = String(body.name).trim() || r.name;
    scheduleSave();
    broadcast('rooms', publicGlobal());
    return sendJSON(res, 200, { ok: true });
  }

  if (req.method === 'POST' && p === '/api/rooms/delete') {
    const r = roomById(body.id);
    if (!r) return sendJSON(res, 404, { error: '房间不存在' });
    if (state.rooms.length <= 1) return sendJSON(res, 400, { error: '至少要保留一个房间' });
    if (state.runner.running && state.runner.roomId === r.id) return sendJSON(res, 409, { error: '该房间正在运行，请先停止' });
    state.rooms = state.rooms.filter((x) => x.id !== r.id);
    if (state.activeRoomId === r.id) state.activeRoomId = state.rooms[0].id;
    await fsp.rm(path.join(TRANSCRIPT_DIR, r.id + '.json'), { force: true }).catch(() => {});
    broadcast('rooms', publicGlobal());
    scheduleSave();
    return sendJSON(res, 200, { ok: true, keptFiles: `room/${r.dir}/` });
  }

  /* ---------------- WorldBox 世界面板 ---------------- */

  if (req.method === 'POST' && p === '/api/worldbox') {
    if (!needRoom()) return;
    if (room.kind !== 'worldbox' && body.enabled !== true) {
      return sendJSON(res, 400, { error: '这不是一个 WorldBox 房间' });
    }
    if (body.enabled === true && room.kind !== 'worldbox') room.kind = 'worldbox';
    const cfg = (room.worldbox ||= { enabled: true, baseUrl: worldbox.DEFAULT_BASE, strictTick: true, nationOf: {} });
    cfg.enabled = true;
    if (body.baseUrl != null) cfg.baseUrl = String(body.baseUrl).trim() || worldbox.DEFAULT_BASE;
    if (body.strictTick != null) cfg.strictTick = Boolean(body.strictTick);
    if (body.maxActionsPerTurn != null && !Number.isNaN(Number(body.maxActionsPerTurn))) {
      cfg.maxActionsPerTurn = Math.max(1, Math.min(9, Math.floor(Number(body.maxActionsPerTurn))));
    }
    if (body.nationOf && typeof body.nationOf === 'object') {
      cfg.nationOf = { ...cfg.nationOf, ...body.nationOf };
      // 清掉指向空值的映射
      for (const [k, v] of Object.entries(cfg.nationOf)) if (!v) delete cfg.nationOf[k];
    }
    if (body.autoAssign) {
      const bridge = worldbox.getBridge(room);
      const nations = await bridge.nations();
      const enabled = room.agents.filter((a) => a.enabled);
      nations.forEach((n, i) => { if (enabled[i]) cfg.nationOf[enabled[i].name] = n['名称']; });
    }
    scheduleSave();
    broadcast('room', publicRoom(room));
    broadcast('rooms', publicGlobal());
    return sendJSON(res, 200, { ok: true, worldbox: await worldboxInfo(room) });
  }

  if (req.method === 'POST' && p === '/api/worldbox/refresh') {
    if (!needRoom()) return;
    const st = await syncWorld(room);
    return sendJSON(res, 200, { ok: !!st, worldbox: await worldboxInfo(room), actions: room.actions.slice(-200) });
  }

  if (req.method === 'POST' && p === '/api/worldbox/action') {
    if (!needRoom()) return;
    const item = room.actions.find((a) => a.id === body.id);
    if (!item) return sendJSON(res, 404, { error: '没有这条提案' });
    const status = ['proposed', 'executed', 'rejected', 'obsolete', 'unparsed'].includes(body.status)
      ? body.status : null;
    if (!status) return sendJSON(res, 400, { error: '状态不合法' });
    item.status = status;
    item.decidedAt = status === 'proposed' ? null : Date.now();
    if (body.note != null) item.note = String(body.note).slice(0, 200);
    if (status === 'executed' || status === 'rejected') {
      const label = status === 'executed' ? '✅ 已执行' : '❌ 被拒绝';
      pushSystem(room, `【创世者裁决】[${item.nation}] ${item.summary} → ${label}${item.note ? `（${item.note}）` : ''}`);
    }
    broadcast('actions', { roomId: room.id, actions: room.actions.slice(-200) });
    broadcast('room', publicRoom(room));
    scheduleSave();
    return sendJSON(res, 200, { ok: true, action: item });
  }

  if (req.method === 'POST' && p === '/api/worldbox/clear') {
    if (!needRoom()) return;
    const keep = Array.isArray(body.keep) ? new Set(body.keep) : new Set(['proposed', 'unparsed']);
    room.actions = room.actions.filter((a) => keep.has(a.status));
    broadcast('actions', { roomId: room.id, actions: room.actions.slice(-200) });
    scheduleSave();
    return sendJSON(res, 200, { ok: true, actions: room.actions });
  }

  /* ---------------- 存档选择 ---------------- */

  // 列出所有可选存档。只读 map.meta，不解析几十 MB 的存档本体。
  if (req.method === 'GET' && p === '/api/worldbox/saves') {
    if (!needRoom()) return;
    const bridge = worldbox.getBridge(room);
    if (!bridge) return sendJSON(res, 400, { error: '这不是一个 WorldBox 房间' });
    const info = await bridge.saves();
    return sendJSON(res, 200, {
      ok: true,
      dataDir: info.dataDir,
      count: info.count,
      selection: info.selection,
      locked: room.worldbox.saveKey || null,
      saves: info.saves,
      error: info.error || null,
    });
  }

  // 切换存档。世界换了 → 之前的国名/国王/提案都可能失效，所以一并处理。
  if (req.method === 'POST' && p === '/api/worldbox/save') {
    if (!needRoom()) return;
    const bridge = worldbox.getBridge(room);
    if (!bridge) return sendJSON(res, 400, { error: '这不是一个 WorldBox 房间' });
    const key = body.key == null ? 'latest' : String(body.key);

    const wasRunning = state.runner.running && state.runner.roomId === room.id;
    if (wasRunning) return sendJSON(res, 409, { error: '房间正在运行，请先停止再切换存档' });

    const sw = await bridge.selectSave(key);
    if (!sw || sw.ok !== true) {
      return sendJSON(res, 400, { error: (sw && sw.error) || '切换存档失败' });
    }

    const locked = (key === 'latest' || key === '') ? null : sw.selected;
    room.worldbox.saveKey = locked;
    room.worldbox.lastFingerprint = null;      // 强制下一轮重新判定世界

    // 换了世界 → 旧提案针对的局势已不存在，连同国名映射一起作废
    room.actions = [];
    const newNation = sw.warning ? null : sw.selected;

    // 国家名单变了：重建领袖与映射
    const nations = await bridge.nations();
    let rebuilt = 0;
    if (nations.length) {
      const before = room.agents.length;
      room.agents = nations.map((n, i) => leaderAgent(n, i));
      room.worldbox.nationOf = Object.fromEntries(
        nations.map((n, i) => [room.agents[i].name, n['名称']]));
      rebuilt = room.agents.length - before;
      if (room.inner) room.inner = {};
      if (room.stats) room.stats = {};
    }

    const st = await syncWorld(room);
    const prompts = await syncWorldboxPrompts(room, { force: true });

    broadcast('agents', publicRoom(room));
    broadcast('actions', { roomId: room.id, actions: [] });
    broadcast('room', publicRoom(room));
    scheduleSave();

    pushSystem(room, `🗂 已切换到存档 ${sw.selected}` +
      (st ? `（${st.worldName} 第 ${st.year} 年，${st.liveKingdoms} 个存活国家）` : '') +
      `。原有提案已作废，领袖名单按新世界重建。`);

    return sendJSON(res, 200, {
      ok: true, locked, switched: sw.selected, warning: sw.warning || null,
      rebuilt, prompts, worldbox: await worldboxInfo(room), room: publicRoom(room),
    });
  }

  // 按当前世界数据重新生成领袖人设（不覆盖使用者手写的 systemPrompt）
  if (req.method === 'POST' && p === '/api/worldbox/prompts') {
    if (!needRoom()) return;
    const force = body.force !== false;
    // 注意：这个变量**不能**叫 res —— 会遮蔽 HTTP 响应对象 res，
    // 导致后面的 sendJSON(res, ...) 报 "res.writeHead is not a function"。
    const filled = await syncWorldboxPrompts(room, { force, postNotice: false });
    return sendJSON(res, 200, { ok: true, result: filled, room: publicRoom(room) });
  }

  /* ---------------- 文件 ---------------- */

  if (req.method === 'POST' && p === '/api/file') {
    if (!needRoom()) return;
    try {
      const { abs, rel } = safePath(room, body.path);
      await fsp.mkdir(path.dirname(abs), { recursive: true });
      await fsp.writeFile(abs, String(body.content ?? ''), 'utf8');
      broadcast('workspace', { roomId: room.id, action: 'write', path: rel });
      return sendJSON(res, 200, { ok: true, path: rel });
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  if (req.method === 'POST' && p === '/api/file/delete') {
    if (!needRoom()) return;
    try {
      const { abs, rel } = safePath(room, body.path);
      await fsp.unlink(abs);
      broadcast('workspace', { roomId: room.id, action: 'delete', path: rel });
      return sendJSON(res, 200, { ok: true, path: rel });
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  /* ---------------- Agent 测试 ---------------- */

  if (req.method === 'POST' && p === '/api/test') {
    const incoming = body.agent || {};
    const stored = incoming.id && room ? room.agents.find((a) => a.id === incoming.id) : null;
    const probe = {
      name: incoming.name || (stored && stored.name) || 'probe',
      model: incoming.model || (stored && stored.model),
      apiBase: incoming.apiBase || (stored && stored.apiBase),
      apiKey: incoming.apiKey || (stored && stored.apiKey) || '',
      temperature: 0,
    };
    try {
      const { content, ms } = await callLLM(probe, [{ role: 'user', content: '只回复两个字：可用' }]);
      return sendJSON(res, 200, { ok: true, model: probe.model, ms, reply: truncate(content.trim(), 60) });
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  /* ---------------- 房间设置 ---------------- */

  if (req.method === 'POST' && p === '/api/room') {
    if (!needRoom()) return;
    const r = room;
    if (body.name != null) r.name = String(body.name).trim() || r.name;
    if (body.setting != null) r.setting = String(body.setting);
    if (body.mode != null && ['round_robin', 'on_demand', 'manual'].includes(body.mode)) r.mode = body.mode;
    for (const k of ['rounds', 'maxTurns', 'maxToolSteps', 'historyLimit']) {
      if (body[k] != null && !Number.isNaN(Number(body[k]))) r[k] = Math.max(0, Number(body[k]));
    }
    if (body.temperature != null && !Number.isNaN(Number(body.temperature))) r.temperature = Number(body.temperature);
    if (body.followMentions != null) r.followMentions = Boolean(body.followMentions);
    if (body.worldbox && typeof body.worldbox === 'object' && r.worldbox) {
      if (body.worldbox.strictTick != null) r.worldbox.strictTick = Boolean(body.worldbox.strictTick);
      if (body.worldbox.baseUrl != null) r.worldbox.baseUrl = String(body.worldbox.baseUrl).trim() || worldbox.DEFAULT_BASE;
      if (body.worldbox.maxActionsPerTurn != null && !Number.isNaN(Number(body.worldbox.maxActionsPerTurn))) {
        r.worldbox.maxActionsPerTurn = Math.max(1, Math.min(9, Math.floor(Number(body.worldbox.maxActionsPerTurn))));
      }
    }
    scheduleSave();
    broadcast('room', publicRoom(r));
    broadcast('rooms', publicGlobal());
    return sendJSON(res, 200, { ok: true, room: publicRoom(r), worldbox: await worldboxInfo(r) });
  }

  /* ---------------- Agent 管理 ---------------- */

  if (req.method === 'POST' && p === '/api/agents') {
    if (!needRoom()) return;
    const a = body.agent || {};
    const isNew = !a.id || !room.agents.some((x) => x.id === a.id);
    const target = isNew
      ? normalizeAgent({
          id: uid(), name: '新角色', emoji: '🤖', color: COLORS[room.agents.length % COLORS.length],
          systemPrompt: '你是一个有自己立场和想法的角色。',
        })
      : room.agents.find((x) => x.id === a.id);

    for (const k of ['name', 'emoji', 'color', 'model', 'apiBase', 'systemPrompt']) {
      if (a[k] != null) target[k] = String(a[k]);
    }
    if (a.temperature != null && !Number.isNaN(Number(a.temperature))) target.temperature = Number(a.temperature);
    if (a.enabled != null) target.enabled = Boolean(a.enabled);
    if (a.canModifyFiles != null) target.canModifyFiles = Boolean(a.canModifyFiles);
    if (typeof a.apiKey === 'string' && a.apiKey !== '__KEEP__') target.apiKey = a.apiKey;

    if (isNew) room.agents.push(target);
    if (!room.stats[target.id]) room.stats[target.id] = { turns: 0, promptTokens: 0, completionTokens: 0, errors: 0 };
    scheduleSave();
    broadcast('agents', { roomId: room.id, agents: room.agents.map((x) => publicAgent(room, x)) });
    return sendJSON(res, 200, { ok: true, agent: publicAgent(room, target) });
  }

  if (req.method === 'POST' && p === '/api/agents/delete') {
    if (!needRoom()) return;
    const i = room.agents.findIndex((x) => x.id === body.id);
    if (i >= 0) room.agents.splice(i, 1);
    delete room.inner[body.id];
    scheduleSave();
    broadcast('agents', { roomId: room.id, agents: room.agents.map((x) => publicAgent(room, x)) });
    return sendJSON(res, 200, { ok: true });
  }

  /* ---------------- 对话 / 运行 ---------------- */

  if (req.method === 'POST' && p === '/api/message') {
    if (!needRoom()) return;
    const text = String(body.text || '').trim();
    if (!text) return sendJSON(res, 400, { error: '消息为空' });
    const msg = pushMessage(room, { role: 'user', name: '用户', emoji: '🧑', color: '#8f9bb3', text });

    if (body.followMentions !== false) {
      for (const id of findMentioned(room, text)) if (!room.queue.includes(id)) room.queue.push(id);
      broadcast('queue', { roomId: room.id, queue: room.queue.slice() });
    }
    if (body.autoStart && !state.runner.running) {
      const q = buildQueue(room, { mode: room.mode, rounds: room.rounds });
      room.queue = room.queue.concat(q);
      broadcast('queue', { roomId: room.id, queue: room.queue.slice() });
      runLoop(room, { force: Boolean(body.force) }); // 不 await
    }
    return sendJSON(res, 200, { ok: true, message: msg, queue: room.queue.slice() });
  }

  if (req.method === 'POST' && p === '/api/run') {
    if (!needRoom()) return;
    if (state.runner.running) {
      return sendJSON(res, 409, { error: `已有房间在运行（${roomById(state.runner.roomId)?.name || '未知'}），请先停止` });
    }
    const mode = body.mode || room.mode;
    room.mode = mode;
    let q = buildQueue(room, {
      mode,
      rounds: body.rounds != null ? body.rounds : room.rounds,
      agentIds: Array.isArray(body.agentIds) && body.agentIds.length ? body.agentIds : null,
    });

    // 按需发言 / 手动点名：队列可能为空，靠判断循环或直接结束
    if (mode === 'on_demand') {
      const enabled = room.agents.filter((a) => a.enabled);
      if (!enabled.length) return sendJSON(res, 400, { error: '没有启用的角色' });
      const hasHistory = room.messages.some((m) => m.role !== 'system');
      if (!hasHistory) q = enabled.map((a) => a.id); // 空房间：先让所有人各说一次开场
    }
    if (mode === 'manual' && !q.length) {
      return sendJSON(res, 400, { error: '手动模式不会自动排队，请点某个角色的「发言」' });
    }
    room.queue = q;
    broadcast('queue', { roomId: room.id, queue: q.slice() });
    if (!q.length && mode !== 'on_demand') return sendJSON(res, 400, { error: '没有可发言的角色（检查是否已启用）' });
    runLoop(room, { force: Boolean(body.force) });
    return sendJSON(res, 200, { ok: true, queue: q, forced: Boolean(body.force) });
  }

  if (req.method === 'POST' && p === '/api/step') {
    if (!needRoom()) return;
    const agent = room.agents.find((a) => a.id === body.agentId);
    if (!agent) return sendJSON(res, 404, { error: '未找到该角色' });
    room.queue.push(agent.id);
    broadcast('queue', { roomId: room.id, queue: room.queue.slice() });
    if (!state.runner.running) runLoop(room, { force: true });   // 手动点名不受世界节拍限制
    return sendJSON(res, 200, { ok: true });
  }

  if (req.method === 'POST' && p === '/api/stop') {
    state.runner.stopRequested = true;
    for (const r of state.rooms) r.queue = [];
    if (state.runner.abort) { try { state.runner.abort.abort(); } catch { /* */ } }
    broadcast('queue', { roomId: state.runner.roomId, queue: [] });
    return sendJSON(res, 200, { ok: true });
  }

  if (req.method === 'POST' && p === '/api/reset') {
    if (!needRoom()) return;
    if (state.runner.running && state.runner.roomId === room.id) return sendJSON(res, 409, { error: '请先停止运行' });
    room.messages = [];
    room.inner = {};
    if (!body.keepStats) room.stats = {};
    broadcast('reset', { roomId: room.id });
    scheduleSave();
    return sendJSON(res, 200, { ok: true });
  }

  return sendJSON(res, 404, { error: 'not found' });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  try {
    if (url.pathname.startsWith('/api/')) await handleApi(req, res, url);
    else await serveStatic(res, url.pathname);
  } catch (e) {
    log('请求出错:', e.message, '\n' + (e.stack || '').split('\n').slice(1, 5).join('\n'));
    if (!res.headersSent) sendJSON(res, 500, { error: e.message });
    else res.end();
  }
});

(async () => {
  await loadAll();
  server.listen(PORT, () => {
    log(`已启动 → http://127.0.0.1:${PORT}`);
    log(`房间数：${state.rooms.length}，工作区根目录：${ROOMS_ROOT}`);
  });
})();

process.on('SIGINT', () => {
  log('正在退出…');
  doSave().finally(() => process.exit(0));
});
