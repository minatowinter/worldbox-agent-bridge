/*
 * 补丁 3：领袖人设区（process.md 5.1 第 2 项）
 *   - 角色卡上显示 自动人设 / 手写人设 / 最终生效（可折叠）+ 生成时的存档与过期标记
 *   - 世界栏加「🪄 重新生成人设」，房间设置里加「重新生成全部 / 只补空缺与过期」+ 统计
 * 注意：服务端早就暴露了 systemPromptAuto / systemPromptAutoFor / effectivePrompt，
 *       所以本补丁**只改前端**，不动 server.js。
 *
 * 用法：node _patch/patch_prompts.js [--dry-run]
 */
const fs = require('fs');
const path = require('path');

const SPACE = 'C:/Users/24771/Desktop/space';
const HERE = __dirname;
const DRY = process.argv.includes('--dry-run');

const fragJs = fs.readFileSync(path.join(HERE, 'insert-app-prompts.js'), 'utf8');
const fragHtml = fs.readFileSync(path.join(HERE, 'insert-index-prompts.html'), 'utf8');

const files = {
  app: path.join(SPACE, 'public/app.js'),
  html: path.join(SPACE, 'public/index.html'),
  css: path.join(SPACE, 'public/style.css'),
};

function info(p) {
  let text = fs.readFileSync(p, 'utf8');
  const bom = text.charCodeAt(0) === 0xfeff;
  if (bom) text = text.slice(1);
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  return { path: p, text, bom, eol, bytes: fs.statSync(p).size };
}
const toEol = (s, eol) => {
  const lf = s.replace(/\r\n/g, '\n');
  return eol === '\r\n' ? lf.replace(/\n/g, '\r\n') : lf;
};

const errors = [];
const notes = [];
function apply(f, label, anchor, replacement) {
  const a = toEol(anchor, f.eol);
  const r = toEol(replacement, f.eol);
  const n = f.text.split(a).length - 1;
  if (n !== 1) { errors.push(`[${label}] 命中 ${n} 次（要求 1）`); return; }
  f.text = f.text.replace(a, r);
  notes.push(`[${label}] ✅ (+${r.length - a.length})`);
}

const app = info(files.app);
const html = info(files.html);
const css = info(files.css);
notes.push(`app.js / index.html / style.css 换行=${app.eol === '\r\n' ? 'CRLF' : 'LF'} 一致=${app.eol === html.eol && html.eol === css.eol}`);

// --- J1：插入人设模块 ---
apply(app, 'J1 插入人设模块',
  `/* ------------------------- 交互：房间设置 ------------------------- */`,
  `${fragJs.replace(/\s*$/, '')}

/* ------------------------- 交互：房间设置 ------------------------- */`);

// --- J2：角色卡上挂人设区 ---
apply(app, 'J2 角色卡挂人设区',
  `        <button class="btn tiny danger" data-act="del" data-id="\${a.id}">删除</button>
      </div>\`;`,
  `        <button class="btn tiny danger" data-act="del" data-id="\${a.id}">删除</button>
      </div>
      \${wbPromptBlock(a)}\`;`);

// --- J3：打开房间面板时统计人设 ---
apply(app, 'J3 面板里统计人设',
  `    // 存档列表走独立接口（/api/state 刻意不带完整列表，只带数量）
    loadWbSaves();`,
  `    // 存档列表走独立接口（/api/state 刻意不带完整列表，只带数量）
    loadWbSaves();
    renderPromptStats();`);

// --- J4：接线 ---
apply(app, 'J4 三个按钮接线',
  `  const sel = $('r_wbSave');
  if (sel) {
    sel.onchange = (e) => switchWbSave(e.target.value);
    $('r_wbSaveReload').onclick = () => loadWbSaves();
  }
}`,
  `  const sel = $('r_wbSave');
  if (sel) {
    sel.onchange = (e) => switchWbSave(e.target.value);
    $('r_wbSaveReload').onclick = () => loadWbSaves();
  }

  // 人设：世界栏一个快捷按钮，房间设置里两个（全部 / 只补空缺与过期）
  const promptBtn = $('worldPromptBtn');
  if (promptBtn) promptBtn.onclick = () => regenPrompts(true);
  const regenForce = $('r_regenForce');
  if (regenForce) regenForce.onclick = () => regenPrompts(true);
  const regenFill = $('r_regenFill');
  if (regenFill) regenFill.onclick = () => regenPrompts(false);
}`);

// --- H2：世界栏加按钮 ---
apply(html, 'H2 世界栏按钮',
  `  <button class="btn tiny ghost" id="worldCfgBtn">🌍 世界接入</button>`,
  `  <button class="btn tiny ghost" id="worldPromptBtn" title="按当前世界数据重新生成领袖人设；你自己写的人设不会被覆盖">🪄 重新生成人设</button>
  <button class="btn tiny ghost" id="worldCfgBtn">🌍 世界接入</button>`);

// --- H3：房间设置里加人设区 ---
apply(html, 'H3 设置面板人设区',
  `            房间正在运行时不允许切换。带 ⚠️ 的是过期存档——游戏没开时数据就停在那里，选之前先看清楚。
          </p>
        </div>`,
  `            房间正在运行时不允许切换。带 ⚠️ 的是过期存档——游戏没开时数据就停在那里，选之前先看清楚。
          </p>
        </div>
${fragHtml.replace(/\s*$/, '')}`);

// --- C2：样式 ---
const cssBlock = `
/* ---- 角色卡上的人设区 ---- */
.agent-prompt { margin-top: 7px; padding-top: 6px; border-top: 1px solid var(--border); }
.prompt-line { font-size: 11px; color: var(--muted); line-height: 1.5; }
.prompt-line.good { color: #7fd6a8; }
.prompt-line.warn { color: #f0c674; }
.prompt-line.none { color: #e2b04a; }
.prompt-block { margin-top: 4px; }
.prompt-block > summary { cursor: pointer; font-size: 11px; color: var(--accent-2); }
.prompt-pre { white-space: pre-wrap; word-break: break-word; font-size: 10.5px; line-height: 1.55;
  background: var(--bg-3); border: 1px solid var(--border); border-radius: 5px;
  padding: 6px 7px; margin: 4px 0 0; max-height: 230px; overflow: auto; color: var(--text);
  font-family: "Segoe UI", system-ui, sans-serif; }
`;
if (css.text.includes('.agent-prompt')) {
  notes.push('[C2 样式] 已存在，跳过（幂等）');
} else {
  const nl = css.text.endsWith('\n') ? '' : '\n';
  css.text = css.text + toEol(nl + cssBlock, css.eol);
  notes.push(`[C2 样式] 追加 ${cssBlock.length} 字符 ✅`);
}

console.log('=== 锚点 ===');
for (const n of notes) console.log('  ' + n);
if (errors.length) {
  console.log('\n中止，未写任何文件：');
  for (const e of errors) console.log('  ✗ ' + e);
  process.exit(2);
}
console.log('\n=== 预览 ===');
for (const f of [app, html, css]) {
  console.log(`  ${path.basename(f.path)}  ${f.bytes} → ${Buffer.byteLength(f.text, 'utf8') + (f.bom ? 3 : 0)}`);
}
if (DRY) { console.log('\n--dry-run：只检查。'); process.exit(0); }

const backupDir = path.join(SPACE, '.backup-prompts');
fs.mkdirSync(backupDir, { recursive: true });
const stamp = String(Date.now());
for (const f of [app, html, css]) {
  fs.copyFileSync(f.path, path.join(backupDir, `${path.basename(f.path)}.${stamp}.bak`));
  fs.writeFileSync(f.path, (f.bom ? '\ufeff' : '') + f.text, 'utf8');
}

console.log('\n=== 写后自检 ===');
const checks = [
  [app.path, 'wbPromptBlock', true], [app.path, 'renderPromptStats', true],
  [app.path, 'regenPrompts', true], [app.path, 'wbSaveKeyOfFingerprint', true],
  [html.path, 'id="worldPromptBtn"', true], [html.path, 'id="r_regenForce"', true],
  [html.path, 'id="r_rPromptStats"', false], [html.path, 'id="r_promptStats"', true],
  [css.path, '.agent-prompt', true],
];
let bad = 0;
for (const [p, needle, want] of checks) {
  const has = fs.readFileSync(p, 'utf8').includes(needle);
  const ok = has === want;
  if (!ok) bad++;
  console.log(`  ${ok ? '✅' : '❌'} ${path.basename(p)} 含 ${needle} = ${has}`);
}
console.log('\n=== 已写入 ===');
for (const f of [app, html, css]) console.log(`  ${f.path}  ${fs.statSync(f.path).size} 字节`);
console.log('备份目录: ' + backupDir);
if (bad) { console.log('\n⚠️ 自检失败，用备份回滚。'); process.exit(3); }
console.log('自检通过 ✅');
