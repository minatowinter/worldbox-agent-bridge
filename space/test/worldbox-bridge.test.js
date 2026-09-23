'use strict';
/* worldbox-bridge 自检：验证工具定义、上下文构建、缓存、降级 */
const b = require('../worldbox-bridge.js');

(async () => {
  const bridge = new b.WorldBridge({ baseUrl: 'http://127.0.0.1:8777' });

  console.log('=== 1. 世界状态 ===');
  const st = await bridge.status();
  if (!st) { console.log('  ✗ 读不到世界状态（服务未启动？）'); process.exit(1); }
  console.log('  世界        :', st.worldName, '第', st.year, '年');
  console.log('  存活国家    :', st.liveKingdoms, '/', st.kingdoms);
  console.log('  存档距今    :', st.saveAgeSeconds.toFixed(0), '秒  stale =', st.stale);
  console.log('  世界指纹    :', String(st.fingerprint).slice(-40));
  console.log('  状态行      :', await bridge.statusLine());

  console.log('\n=== 2. 节拍器 ===');
  console.log('  hasAdvanced(null)      =', await bridge.hasAdvanced(null));
  console.log('  hasAdvanced(当前状态)  =', await bridge.hasAdvanced(st));

  console.log('\n=== 3. 国家解析 ===');
  for (const t of [6, '#6', '大仁', '大', '莱国', '不存在的国家']) {
    const r = await bridge.resolve(t);
    console.log(`  ${JSON.stringify(t).padEnd(18)} -> ${r ? r['名称'] + (r.ambiguous ? ' (歧义:' + r.ambiguous.join('/') + ')' : '') : 'null'}`);
  }

  console.log('\n=== 4. 角色上下文 ===');
  const ctx = await bridge.contextFor('大仁');
  const raw = JSON.stringify(ctx, null, 1);
  console.log('  体积        :', Buffer.byteLength(raw, 'utf8'), '字节');
  console.log('  顶层键      :', Object.keys(ctx).join(', '));
  console.log('  我的国家    :', ctx['我的国家']['名称'],
              '人口', ctx['我的国家']['人口'], '军队', ctx['我的国家']['军队'],
              '国库', ctx['我的国家']['国库']);
  console.log('  城市(前3)   :', JSON.stringify(ctx['我的国家']['城市列表_按无家可归排序_仅前8座'].slice(0, 3)));
  console.log('  趋势_人口   :', JSON.stringify(ctx['趋势']['人口']));
  console.log('  列国        :', ctx['列国'].length
    ? ctx['列国'].map(n => `${n['名称']}(军${n['军队']})`).join(' ')
    : '（这个世界只有你一个国家）');
  if (ctx['列国'].length) {
    console.log('  列国城市    :', JSON.stringify(ctx['列国'][0]['城市列表']));
  } else {
    console.log('  [跳过] 单国世界没有他国数据，属正常情况');
  }
  console.log('  警告        :', ctx['数据新鲜度']['警告']);

  console.log('\n=== 5. 系统提示注入 ===');
  const room = { id: 'r_test', name: '世界频道', worldbox: { enabled: true, nationOf: { '大仁王': '大仁' } } };
  const agent = { id: 'a1', name: '大仁王' };
  const section = await b.systemSection(room, agent, bridge);
  console.log('  段落体积    :', Buffer.byteLength(section, 'utf8'), '字节');
  console.log('  首行        :', section.split('\n')[0]);
  console.log('  含国策格式  :', section.includes('请创世者'));
  console.log('  含趋势要求  :', section.includes('必须看趋势'));

  console.log('\n=== 6. 未指定国家的降级 ===');
  const s2 = await b.systemSection({ worldbox: { enabled: true } }, { name: 'x' }, bridge);
  console.log('  ', s2.split('\n')[1].slice(0, 60));

  console.log('\n=== 7. 非世界房间应完全不受影响 ===');
  console.log('  bridgeEnabled(普通房间) =', b.bridgeEnabled({ id: 'r2' }));
  console.log('  getBridge(普通房间)     =', b.getBridge({ id: 'r2' }));
  console.log('  systemSection 返回空串 =', JSON.stringify(await b.systemSection({ id: 'r2' }, { name: 'x' }, bridge)));

  console.log('\n=== 8. 世界服务不可用时的降级 ===');
  const dead = new b.WorldBridge({ baseUrl: 'http://127.0.0.1:59999', timeoutMs: 1500 });
  console.log('  status()      =', await dead.status());
  console.log('  statusLine()  =', await dead.statusLine());
  const deadCtx = await dead.contextFor('大仁');
  console.log('  contextFor()  =', JSON.stringify(deadCtx).slice(0, 90));
  const deadSection = await b.systemSection({ worldbox: { enabled: true, nationOf: { x: '大仁' } } }, { name: 'x' }, dead);
  console.log('  注入段落含警告:', deadSection.includes('读不到世界情报'));

  console.log('\n=== 9. 工具定义（塞进 TOOLS 的形态） ===');
  console.log('  write 标志  :', b.TOOL_DEF.write, '(false = 只读角色也能用)');
  console.log('  desc 首段   :', b.TOOL_DEF.desc.slice(0, 50) + '…');
  const toolResult = await b.TOOL_DEF.run(room, { nation: '莱国' });
  console.log('  run() 返回  :', Buffer.byteLength(toolResult, 'utf8'), '字节, 合法 JSON =',
              (() => { try { JSON.parse(toolResult); return true; } catch { return false; } })());

  console.log('\n=== 10. 缓存生效 ===');
  const t0 = Date.now();
  for (let i = 0; i < 20; i++) await bridge.status();
  console.log('  连续 20 次 status() 耗时:', Date.now() - t0, 'ms（缓存命中应 <100ms）');

  console.log('\n全部自检完成。');
  bridge.dispose();
  dead.dispose();
})().catch((e) => { console.error('自检失败:', e); process.exit(1); });
