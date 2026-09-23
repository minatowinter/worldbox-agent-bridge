'use strict';
/*
 * 集成测试：模拟一次完整的"角色发言 → 产出政策 → 解析动作 → 玩家裁决 → 回填"
 * 闭环，验证 worldbox-bridge 与 server.js 的四处改动能真正串起来。
 *
 * 它不启动真的 server.js，而是按 server.js 的接线方式复现 buildMessages 的
 * 组装逻辑 + TOOLS 执行 + 政策解析，确认端到端可用。
 * 这是给接入方看的最有说服力的一份验证。
 */

const assert = require('node:assert');
const worldbox = require('../worldbox-bridge.js');

let pass = 0, fail = 0;
function check(label, cond, extra = '') {
  if (cond) { pass++; console.log(`  [通过] ${label} ${extra}`); }
  else { fail++; console.log(`  [失败] ${label} ${extra}`); }
}

/* ---------------- 复现 server.js 的接线 ---------------- */

// —— 改动 2：TOOLS 里注册 world_state（模拟 server.js 的 TOOLS 表）
const TOOLS = {
  read_file: { write: false, desc: '读取文件', run: async () => '(文件内容)' },
  write_file: { write: true, desc: '写入文件', run: async () => '已写入' },
  world_state: worldbox.TOOL_DEF,
};

// —— 改动 3：toolProtocolFor 按房间条件过滤
function toolProtocolFor(agent, room) {
  const canWrite = agent.canModifyFiles !== false;
  const available = Object.entries(TOOLS).filter(([n, t]) =>
    (canWrite || !t.write) && (n !== 'world_state' || worldbox.bridgeEnabled(room)));
  return available.map(([n, t]) => `- ${n}：${t.desc}`).join('\n');
}

// —— 改动 4：buildMessages 注入世界局势
async function buildMessages(room, agent) {
  const wx = worldbox.bridgeEnabled(room)
    ? await worldbox.systemSection(room, agent, worldbox.getBridge(room))
    : '';
  const system = [
    agent.systemPrompt,
    '',
    '## 房间信息',
    `- 房间名：${room.name}`,
    `- 你是：${agent.name}`,
    '',
    toolProtocolFor(agent, room),
    '',
    ...(wx ? [wx, ''] : []),
    '## 发言准则',
    '- 用中文，像在群里说话一样自然。',
  ].join('\n');
  return [{ role: 'system', content: system }];
}

// —— 复现 executeTool
async function executeTool(room, agent, name, args) {
  const tool = TOOLS[name];
  if (!tool) return { ok: false, result: `未知工具：${name}` };
  if (tool.write && agent.canModifyFiles === false) return { ok: false, result: '无权限' };
  try { return { ok: true, result: String(await tool.run(room, args || {})) }; }
  catch (e) { return { ok: false, result: `执行失败：${e.message}` }; }
}

/* ---------------- 政策解析（对应指南第六节） ---------------- */

const SITE_RE = /执行[^\n:：]*[:：][^\n]*?([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{1,20}?)\s*[（(]\s*(\d{1,4})\s*[,，]\s*(\d{1,4})\s*[)）]([^\n]*)/;
const SWITCH_RE = /执行\s*[（(]\s*全国开关\s*[)）]\s*[:：]\s*([^\n]+)/;

function parsePolicyActions(text, nation) {
  const out = [];
  for (const line of String(text).split('\n')) {
    const s = line.trim();
    if (!s.includes('执行')) continue;
    let m = SWITCH_RE.exec(s);
    if (m) { out.push({ nation, kind: '开关型', target: null, coord: null, summary: m[1].trim() }); continue; }
    m = SITE_RE.exec(s);
    if (m) {
      out.push({ nation, kind: '落点型', target: m[1].trim(),
                 coord: [Number(m[2]), Number(m[3])], summary: m[1].trim() + m[4].trim() });
    }
  }
  return out;
}

/* ---------------- 动作账本 ---------------- */

let seq = 0;
const journal = [];
function propose(a, fingerprint) {
  const item = { id: `a${++seq}`, ...a, status: 'proposed', fingerprint, note: '' };
  journal.push(item); return item;
}
function decide(id, status, note) {
  const it = journal.find((x) => x.id === id);
  if (it) { it.status = status; it.note = note; }
  return it;
}
function expireStale(fp) {
  return journal.filter((x) => x.status === 'proposed' && x.fingerprint !== fp)
                .map((x) => { x.status = 'obsolete'; x.note = '世界已推进'; return x; });
}

/* ---------------- 开始测试 ---------------- */

(async () => {
  const bridge = new worldbox.WorldBridge({ baseUrl: 'http://127.0.0.1:8777' });

  console.log('=== 0. 前置：世界服务可用 ===');
  const st = await bridge.status();
  if (!st) { console.log('  ✗ 世界服务不可用，请先运行 python worldbox_agent.py serve'); process.exit(1); }
  check('读到世界状态', true, `=${st.worldName} 第 ${st.year} 年`);

  const wbRoom = {
    id: 'r_wb', name: 'WorldBox 世界频道',
    worldbox: { enabled: true, baseUrl: 'http://127.0.0.1:8777', strictTick: true,
                nationOf: { 大仁王: '大仁', 莱国公: '莱国' } },
  };
  const plainRoom = { id: 'r_plain', name: '普通房间' };
  const king = { id: 'a1', name: '大仁王', systemPrompt: '你是大仁的国王，谨慎务实。', canModifyFiles: true };
  const reader = { id: 'a2', name: '旁观者', systemPrompt: '你只观察。', canModifyFiles: false };

  console.log('\n=== 1. 普通房间不应受影响（改动 3 的隔离性） ===');
  const plainProto = toolProtocolFor(king, plainRoom);
  check('普通房间看不到 world_state', !plainProto.includes('world_state'));
  check('普通房间仍有原有工具', plainProto.includes('read_file') && plainProto.includes('write_file'));
  const plainMsgs = await buildMessages(plainRoom, king);
  check('普通房间不注入世界局势', !plainMsgs[0].content.includes('WorldBox 世界局势'));
  check('普通房间提示体积正常', Buffer.byteLength(plainMsgs[0].content, 'utf8') < 1200,
        `= ${Buffer.byteLength(plainMsgs[0].content, 'utf8')} 字节`);

  console.log('\n=== 2. WorldBox 房间注入世界局势（改动 4） ===');
  const msgs = await buildMessages(wbRoom, king);
  const sys = msgs[0].content;
  check('注入了世界局势段落', sys.includes('## WorldBox 世界局势'));
  check('注入段含国策格式要求', sys.includes('请创世者'));
  check('注入段含趋势要求', sys.includes('必须看趋势'));
  check('注入段含"只能提议"约束', sys.includes('只能提议'));
  check('注入段含坐标纪律', sys.includes('坐标'));
  check('strictTick 附加了节拍要求', sys.includes('世界指纹'));
  check('提示体积可控', Buffer.byteLength(sys, 'utf8') < 12000,
        `= ${Buffer.byteLength(sys, 'utf8')} 字节`);
  check('注入的是本国的数据', sys.includes('大仁') && sys.includes('北平'));
  check('数据过期时带警告', st.stale ? sys.includes('分钟旧') : true,
        st.stale ? '(当前数据确实过期，警告已出现)' : '(数据新鲜)');

  console.log('\n=== 3. 工具可见性按权限正确过滤（改动 3） ===');
  const kingProto = toolProtocolFor(king, wbRoom);
  const readerProto = toolProtocolFor(reader, wbRoom);
  check('可写角色能看到 world_state', kingProto.includes('world_state'));
  check('只读角色也能看到 world_state', readerProto.includes('world_state'),
        '(world_state 是只读工具)');
  check('只读角色看不到 write_file', !readerProto.includes('write_file'));
  check('只读角色仍能看到 read_file', readerProto.includes('read_file'));

  console.log('\n=== 4. 工具真的能跑（改动 2） ===');
  // 国家名不能硬编码：不同存档里的世界完全可能只剩一个国家，
  // 或者国家改名。全部从当前世界动态取。
  const allNations = await bridge.nations();
  const aliveNations = allNations.filter((n) => n['是否存续']);
  const selfNation = (aliveNations[0] || allNations[0])['名称'];
  const otherNation = (aliveNations.find((n) => n['名称'] !== selfNation) || {}).名称;
  console.log(`        当前世界国家: ${allNations.map((n) => n['名称']).join('、')}`);

  const r1 = await executeTool(wbRoom, king, 'world_state', {});
  check('world_state 无参调用成功', r1.ok, `= ${Buffer.byteLength(r1.result, 'utf8')} 字节`);
  const ctxDefault = JSON.parse(r1.result);
  check('默认取到自己的国家', ctxDefault['我的国家']['名称'] === selfNation,
        `= ${ctxDefault['我的国家']['名称']}`);

  if (otherNation) {
    const r2 = await executeTool(wbRoom, king, 'world_state', { nation: otherNation });
    const ctxOther = JSON.parse(r2.result);
    check('可指定查询他国', ctxOther['我的国家']['名称'] === otherNation);
    check('他国数据含城市坐标',
          Array.isArray(ctxOther['我的国家']['城市列表_按无家可归排序_仅前8座'][0]['坐标']));
  } else {
    console.log('        [跳过] 这个世界只有 1 个国家，无法测试查询他国');
    check('单国世界查询他国名会明确报错',
          (await executeTool(wbRoom, king, 'world_state', { nation: '不存在的国家' }))
            .result.includes('找不到国家'));
  }
  const r3 = await executeTool(wbRoom, king, 'world_state', { nation: '不存在的国家' });
  check('查不到国家时给出可用列表',
        r3.result.includes('找不到国家') || r3.result.includes('世界服务不可用'));
  check('可用列表里含真实国名', r3.result.includes(selfNation));
  const r4 = await executeTool(plainRoom, king, 'world_state', {});
  check('普通房间调用该工具会说明未接入', r4.result.includes('未接入'));

  console.log('\n=== 5. 端到端闭环：角色产出 → 解析 → 裁决 → 回填 ===');
  // 模拟角色按 skill 格式产出的一份国策
  const policy = `## 第 ${st.year} 年 · 大仁国策

### 零、情报状态
存档距今约 4 小时，可能已过期。

### 二、国策
**A. 落点型措施**
1. **补住房** —— 目标：消除无家可归
   - 执行：请创世者在 北平(4,23) 增建/升级住房，容量 +83
2. **压饥饿** —— 执行：请创世者在 荆州(21,8) 投放食物约 25 人份
**B. 开关型措施**
3. **减税** —— 执行（全国开关）：把「地方高税」下调一档，试行一年

### 四、内心
我不想再打下去了。`;
  const actions = parsePolicyActions(policy, '大仁');
  check('从国策抽出 3 条动作', actions.length === 3, `= ${actions.length}`);
  const site = actions.filter((a) => a.kind === '落点型');
  const sw = actions.filter((a) => a.kind === '开关型');
  check('落点型 2 条', site.length === 2);
  check('开关型 1 条', sw.length === 1);
  check('坐标解析正确', JSON.stringify(site.map((a) => a.coord)) === '[[4,23],[21,8]]',
        JSON.stringify(site.map((a) => a.coord)));
  check('城市名解析正确', site.map((a) => a.target).join(',') === '北平,荆州');
  check('开关型无坐标', sw[0].coord === null && sw[0].target === null);

  // 入账
  const proposed = actions.map((a) => propose(a, st.fingerprint));
  check('动作入账', journal.filter((x) => x.status === 'proposed').length === 3);

  // 玩家裁决
  decide(proposed[0].id, 'executed', '已在北平增建 90 人份住房');
  decide(proposed[2].id, 'rejected', '今年不改税率');
  check('裁决已回填', proposed[0].status === 'executed' && proposed[2].status === 'rejected');
  check('待执行清单剩 1 条', journal.filter((x) => x.status === 'proposed').length === 1);

  // 世界推进 → 旧提案作废
  const expired = expireStale('新指纹|999999');
  check('世界推进后旧提案作废', expired.length === 1 && expired[0].status === 'obsolete',
        `(作废 ${expired.length} 条)`);

  // 回填给角色（下一轮）
  const feedback = journal
    .filter((x) => x.status !== 'proposed')
    .map((x) => `- ${x.summary} → ${{ executed: '已执行', rejected: '被拒绝', obsolete: '已过期' }[x.status]}（${x.note}）`)
    .join('\n');
  check('可生成给角色的裁决回执', feedback.includes('已执行') && feedback.includes('被拒绝'),
        '');
  console.log('       回执预览：');
  feedback.split('\n').forEach((l) => console.log('         ' + l));

  console.log('\n=== 6. 节拍器：同帧不重复决策 ===');
  const st2 = await bridge.status();
  check('同一帧 hasAdvanced = false', (await bridge.hasAdvanced(st2)) === false);
  check('新指纹 hasAdvanced = true', (await bridge.hasAdvanced({ fingerprint: 'other' })) === true);

  console.log('\n=== 7. 降级：世界服务不可用时不崩 ===');
  const deadBridge = new worldbox.WorldBridge({ baseUrl: 'http://127.0.0.1:59998', timeoutMs: 1200 });
  const deadRoom = { worldbox: { enabled: true, nationOf: { 大仁王: '大仁' } } };
  const deadMsgs = await buildMessages(deadRoom, { ...king, id: 'a9' }).catch((e) => [{ content: 'THREW:' + e.message }]);
  // 注意：getBridge 按 room.worldbox.baseUrl 取，这里没有 baseUrl 会退回默认端口 → 仍可用
  check('buildMessages 不会抛异常', !deadMsgs[0].content.startsWith('THREW'),
        deadMsgs[0].content.startsWith('THREW') ? deadMsgs[0].content : '');
  const deadSection = await worldbox.systemSection(
    { worldbox: { enabled: true, baseUrl: 'http://127.0.0.1:59998', nationOf: { x: '大仁' } } },
    { name: 'x' }, deadBridge);
  check('服务不可用时给出明确提示', deadSection.includes('读不到世界情报'));
  check('提示里没有编造的数据', !deadSection.includes('"人口"'));

  console.log('\n=== 8. 存档选择（多存档场景） ===');
  const savesInfo = await bridge.saves();
  console.log(`        数据目录: ${savesInfo.dataDir}`);
  console.log(`        可用存档: ${savesInfo.count} 份`);
  check('能列出存档', savesInfo.saves.length > 0, `= ${savesInfo.saves.length} 份`);
  check('每份带世界名与新鲜度',
        savesInfo.saves.every((r) => 'world_name' in r && 'age_text' in r));
  check('每份带 selected 标记', savesInfo.saves.every((r) => 'selected' in r));

  const currentKey = (await bridge.status()).fingerprint.split('|')[0].split(/[\\/]/).slice(-2)[0];
  const usedSave = savesInfo.saves.find((r) => r.selected);
  console.log(`        当前使用: ${usedSave ? usedSave.key + ' (' + usedSave.age_text + ')' : '(未标记)'}`);

  if (savesInfo.saves.length > 1) {
    const other = savesInfo.saves.find((r) => !r.selected) || savesInfo.saves[1];
    const oldWorld = (await bridge.status()).worldName;
    const sw = await bridge.selectSave(other.key);
    check('切换存档成功', sw.ok === true, `-> ${sw.selected}`);
    const after = await bridge.status();
    check('切换后确实换了存档', after.fingerprint !== st.fingerprint,
          `world=${after.worldName} year=${after.year}`);
    console.log(`        切换后: ${after.worldName} 第 ${after.year} 年，` +
                `存活 ${after.liveKingdoms}/${after.kingdoms} 国`);
    // 换回来
    const back = await bridge.selectSave('latest');
    check('可恢复跟随最新', back.ok === true, `-> ${back.selected}`);
    // 恢复后错误选择要能被发现
    const bad = await bridge.selectSave('绝对不存在的存档名');
    check('无效存档名会给出回退警告', Boolean(bad.warning), (bad.warning || '').slice(0, 50));
    await bridge.selectSave('latest');
  } else {
    console.log('        [跳过] 只有一份存档，无法测试切换');
  }

  console.log('\n=== 9. 自动填充角色 system prompt ===');
  const fillRoom = {
    id: 'r_fill', name: '自动人设测试',
    worldbox: { enabled: true, nationOf: { 甲: selfNation, 乙: '不存在的国家' } },
    agents: [
      { id: 'f1', name: '甲', systemPrompt: '' },              // 空白 → 应生成
      { id: 'f2', name: '乙', systemPrompt: '' },              // 国家不存在 → 跳过
      { id: 'f3', name: '丙', systemPrompt: '我是手写的人设' },  // 无国家映射 → 跳过
      { id: 'f4', name: '丁', systemPrompt: '手写优先',
        worldboxNation: selfNation },                          // 手写 → 保留
    ],
  };
  const fill = await worldbox.autofillSystemPrompts(fillRoom, bridge);
  console.log(`        生成 ${fill.filled}，重算 ${fill.refreshed}，跳过 ${fill.skipped}`);
  fill.details.forEach((d) => console.log(`          ${d.agent}: ${d.action}${d.reason ? '（' + d.reason + '）' : ''}`));
  check('空白人设被自动生成', fill.filled >= 1, `= ${fill.filled}`);
  check('生成的内容写进了 systemPromptAuto',
        String(fillRoom.agents[0].systemPromptAuto || '').length > 50);
  check('生成内容含真实国名', String(fillRoom.agents[0].systemPromptAuto).includes(selfNation));
  check('生成内容含世界名', String(fillRoom.agents[0].systemPromptAuto).includes(st.worldName));
  check('生成内容含数据纪律',
        String(fillRoom.agents[0].systemPromptAuto).includes('绝不编造'));
  check('记录了生成时的世界指纹',
        fillRoom.agents[0].systemPromptAutoFor === (await bridge.status()).fingerprint);
  check('手写人设未被覆盖',
        fillRoom.agents[3].systemPromptAuto === undefined
        && fillRoom.agents[3].systemPrompt === '手写优先');
  check('找不到国家时跳过而非编造',
        fill.details.some((d) => d.agent === '乙' && d.action === '跳过'));

  // 世界未变时重复调用不应重算
  const again = await worldbox.autofillSystemPrompts(fillRoom, bridge);
  check('世界未变时不重复生成', again.filled === 0 && again.refreshed === 0,
        `filled=${again.filled} refreshed=${again.refreshed}`);

  // 世界指纹变了 → 应重算
  fillRoom.agents[0].systemPromptAutoFor = 'old|fingerprint';
  const afterWorldChange = await worldbox.autofillSystemPrompts(fillRoom, bridge);
  check('世界变化后重新生成', afterWorldChange.refreshed === 1,
        `refreshed=${afterWorldChange.refreshed}`);

  // 普通房间不应生成
  const plainFill = await worldbox.autofillSystemPrompts(
    { id: 'r2', agents: [{ name: 'x', systemPrompt: '' }] }, bridge);
  check('普通房间不生成人设', plainFill.filled === 0 && plainFill.skipped === 0);

  console.log('\n=== 10. 人设优先级与拼接 ===');
  check('只有手写时用手写',
        worldbox.rolePromptFor({ systemPrompt: 'A' }) === 'A');
  check('只有自动时用自动',
        worldbox.rolePromptFor({ systemPromptAuto: 'B' }) === 'B');
  check('两者都有时拼接（手写在前）',
        worldbox.rolePromptFor({ systemPrompt: 'A', systemPromptAuto: 'B' }) === 'A\n\nB');
  check('都没有时返回空串', worldbox.rolePromptFor({}) === '');

  console.log(`\n===== 结果：通过 ${pass} 项，失败 ${fail} 项 =====`);
  // 释放 keep-alive 连接，让进程干净退出（否则某些 Node 版本会触发 libuv 断言）
  bridge.dispose();
  deadBridge.dispose();
  process.exitCode = fail ? 1 : 0;
})().catch((e) => { console.error('集成测试异常：', e); process.exit(1); });
