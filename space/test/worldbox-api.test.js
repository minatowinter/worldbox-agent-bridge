'use strict';
/*
 * 实机验证：直接对运行中的 server.js 打 HTTP，确认新增的
 * 「存档选择」与「按世界数据生成领袖人设」两个功能真的可用。
 *
 * 与 worldbox-integration.test.js 的区别：那个测的是桥接模块的逻辑，
 * 这个测的是 server.js 里真实的 API 端点与房间状态变更。
 *
 * 用法： node test/worldbox-api.test.js [端口]     默认 8799
 */

const BASE = `http://127.0.0.1:${process.argv[2] || 8799}`;
let pass = 0, fail = 0;

function check(label, cond, extra = '') {
  if (cond) { pass++; console.log(`  [通过] ${label} ${extra}`); }
  else { fail++; console.log(`  [失败] ${label} ${extra}`); }
}

async function api(method, p, body) {
  const res = await fetch(BASE + p, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  return { status: res.status, data };
}

const get = (p) => api('GET', p);
const post = (p, b) => api('POST', p, b || {});

(async () => {
  console.log(`=== 0. 房间程序可达性 (${BASE}) ===`);
  const st = await get('/api/state');
  if (st.status !== 200) {
    console.log(`  ✗ 无法连接房间程序（HTTP ${st.status}）。请先启动 node server.js`);
    process.exit(1);
  }
  const active = st.data.active;
  check('拿到房间状态', !!active, `房间=${active && active.name}`);
  console.log(`        类型: ${active.kind}  角色: ${active.agents.map((a) => a.name).join('、')}`);
  check('这是一个 WorldBox 房间', active.kind === 'worldbox');

  console.log('\n=== 1. 世界面板数据 /api/worldbox ===');
  const wb = (await get('/api/worldbox')).data;
  check('面板可用', wb.enabled === true, `line=${wb.line || ''}`);
  console.log(`        ${wb.line}`);
  check('带回存档列表', Array.isArray(wb.saves) && wb.saves.length > 0,
        `= ${wb.savesCount} 份`);
  check('存档项字段完整',
        wb.saves.every((s) => 'key' in s && 'world' in s && 'age' in s && 'selected' in s));
  check('带回数据目录', typeof wb.dataDir === 'string' && wb.dataDir.length > 0);
  const cur = wb.saves.find((s) => s.selected);
  console.log(`        当前存档: ${cur ? cur.key + ' / ' + cur.world + ' / ' + cur.age : '(未标记)'}`);

  console.log('\n=== 2. 存档列表端点 /api/worldbox/saves ===');
  const sv = (await get('/api/worldbox/saves')).data;
  check('端点返回 ok', sv.ok === true);
  check('数量一致', sv.count === wb.savesCount, `= ${sv.count}`);
  check('带当前锁定状态字段', 'locked' in sv, `locked=${sv.locked}`);

  console.log('\n=== 3. 领袖人设生成 /api/worldbox/prompts ===');
  const before = (await get('/api/state')).data.active;
  const manuallySet = before.agents.filter((a) => String(a.systemPrompt || '').trim()).length;
  console.log(`        当前有 ${manuallySet} 位领袖是手写人设`);
  const pf = (await post('/api/worldbox/prompts', { force: false })).data;
  check('端点返回 ok', pf.ok === true);
  console.log(`        生成 ${pf.result.filled}，重算 ${pf.result.refreshed}，跳过 ${pf.result.skipped}`);
  pf.result.details.forEach((d) => console.log(`          ${d.agent}: ${d.action}${d.reason ? '（' + d.reason + '）' : ''}`));
  check('手写人设未被覆盖', pf.result.filled === 0 || manuallySet === 0);

  // 强制生成，验证内容质量
  const forced = (await post('/api/worldbox/prompts', { force: true })).data;
  check('强制生成成功', forced.ok === true);
  const after = (await get('/api/state')).data.active;
  const withAuto = after.agents.filter((a) => String(a.systemPromptAuto || '').trim());
  check('有角色拿到了自动人设', withAuto.length > 0, `= ${withAuto.length} 位`);
  if (withAuto.length) {
    const sample = withAuto[0];
    console.log(`        --- ${sample.name} 的自动人设（前 200 字）---`);
    console.log('        ' + String(sample.systemPromptAuto).slice(0, 200).replace(/\n/g, '\n        '));
    check('人设含世界名', /WorldBox 世界/.test(sample.systemPromptAuto));
    check('人设含数据纪律', /绝不编造/.test(sample.systemPromptAuto));
    check('记录了世界指纹', !!sample.systemPromptAutoFor);
  }

  console.log('\n=== 4. 切换存档 /api/worldbox/save ===');
  const target = sv.saves.find((s) => !s.selected) || sv.saves[1];
  if (!target) {
    console.log('        [跳过] 只有一份存档');
  } else {
    const sw = (await post('/api/worldbox/save', { key: target.key })).data;
    check('切换成功', sw.ok === true, `-> ${sw.switched}`);
    check('锁定了新存档', sw.locked === target.key, `locked=${sw.locked}`);
    check('重建了领袖名单', typeof sw.rebuilt === 'number', `rebuilt=${sw.rebuilt}`);
    console.log(`        切换后世界: ${sw.worldbox.status ? sw.worldbox.status.worldName + ' 第 ' + sw.worldbox.status.year + ' 年，存活 ' + sw.worldbox.status.liveKingdoms + ' 国' : '?'}`);
    const after2 = (await get('/api/state')).data.active;
    check('房间的 saveKey 已持久化到面板数据', after2.worldbox.saveKey === target.key,
          `= ${after2.worldbox.saveKey}`);
    check('领袖已按新世界重建', after2.agents.length > 0,
          `= ${after2.agents.length} 位: ${after2.agents.map((a) => a.name).join('、')}`);
    // 面板详情端点同样要带 saveKey，且 /api/state 不应携带整份存档列表
    const detail = (await get('/api/worldbox')).data;
    check('详情端点也带 saveKey', detail.saveKey === target.key, `= ${detail.saveKey}`);
    check('/api/state 不携带完整存档列表（避免每回合臃肿）',
          !Array.isArray(after2.worldbox.saves));
    check('/api/state 仍报告存档数量', typeof after2.worldbox.savesCount === 'number',
          `= ${after2.worldbox.savesCount}`);
    check('详情端点带完整存档列表', Array.isArray(detail.saves) && detail.saves.length > 0,
          `= ${detail.saves && detail.saves.length}`);

    // 磁盘上的 config.json 也要记下来，重启不丢
    await new Promise((r) => setTimeout(r, 600));   // 等 scheduleSave 落盘
    let cfgOnDisk = null;
    try { cfgOnDisk = JSON.parse(require('node:fs').readFileSync(
      require('node:path').join(__dirname, '..', 'data', 'config.json'), 'utf8')); } catch {}
    const roomOnDisk = cfgOnDisk && cfgOnDisk.rooms.find((r) => r.id === after2.id);
    check('config.json 里也记下了 saveKey（重启不丢）',
          !!(roomOnDisk && roomOnDisk.worldbox && roomOnDisk.worldbox.saveKey === target.key),
          roomOnDisk && roomOnDisk.worldbox ? `= ${roomOnDisk.worldbox.saveKey}` : '(读不到)');

    // 恢复跟随最新
    const back = (await post('/api/worldbox/save', { key: 'latest' })).data;
    check('可恢复跟随最新', back.ok === true, `-> ${back.switched}`);
    check('恢复后 locked 为空', !back.locked, `locked=${back.locked}`);
  }

  console.log('\n=== 5. 错误处理 ===');
  const bad = await post('/api/worldbox/save', { key: '绝对不存在的存档' });
  check('无效存档名返回结构化结果', bad.status === 200 || bad.status === 400,
        `HTTP ${bad.status}`);
  check('无效存档名带警告或错误',
        Boolean(bad.data.warning || bad.data.error),
        (bad.data.warning || bad.data.error || '').slice(0, 50));

  console.log('\n=== 6. 普通房间不受影响 ===');
  const created = (await post('/api/rooms/create', { kind: 'normal', name: '回归测试-普通房间' })).data;
  check('能建普通房间', created.ok === true, `id=${created.room && created.room.id}`);
  if (created.ok) {
    const plainId = created.room.id;
    const plainWb = (await get('/api/worldbox')).data;
    check('普通房间的面板为 enabled:false', plainWb.enabled === false);
    const plainSaves = await get('/api/worldbox/saves');
    check('普通房间的存档端点是可控错误', plainSaves.status === 400,
          `HTTP ${plainSaves.status}`);
    // 清理
    await post('/api/rooms/delete', { id: plainId });
    console.log('        已删除测试房间');
  }

  console.log(`\n===== 结果：通过 ${pass} 项，失败 ${fail} 项 =====`);
  process.exitCode = fail ? 1 : 0;
})().catch((e) => { console.error('实机验证异常：', e); process.exit(1); });
