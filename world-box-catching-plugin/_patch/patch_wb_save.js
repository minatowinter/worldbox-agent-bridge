/*
 * 把「世界存档选择器」补进 space 前端。
 *
 * 设计要点：
 *  1) 每个锚点必须**恰好命中一次**，否则整体中止、不写任何文件（避免半吊子状态）。
 *  2) 自动适配目标文件的换行风格（CRLF / LF）与 BOM。
 *  3) 写之前先备份到 space\.backup-savepick\，可一键回滚。
 *  4) 写之后重新读回来自检，并交给调用方用 node --check 做语法校验。
 *
 * 用法：
 *   node _patch/patch_wb_save.js --dry-run    # 只校验锚点，不写
 *   node _patch/patch_wb_save.js              # 真写
 */
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const SPACE = 'C:/Users/24771/Desktop/space';
const DRY = process.argv.includes('--dry-run');

const targets = {
  app: path.join(SPACE, 'public/app.js'),
  html: path.join(SPACE, 'public/index.html'),
  css: path.join(SPACE, 'public/style.css'),
};

const fragApp = fs.readFileSync(path.join(HERE, 'insert-app-module.js'), 'utf8');
const fragHtml = fs.readFileSync(path.join(HERE, 'insert-index-section.html'), 'utf8');

/* ---------- 读文件（记住 BOM 与换行风格） ---------- */
function readFileInfo(p) {
  let text = fs.readFileSync(p, 'utf8');
  const bom = text.charCodeAt(0) === 0xfeff;
  if (bom) text = text.slice(1);
  const crlf = text.includes('\r\n');
  return { path: p, text, bom, crlf, eol: crlf ? '\r\n' : '\n', bytes: fs.statSync(p).size };
}

/** 把片段的换行统一成目标文件的风格。 */
function toEol(s, eol) {
  const lf = s.replace(/\r\n/g, '\n');
  return eol === '\r\n' ? lf.replace(/\n/g, '\r\n') : lf;
}

const errors = [];
const notes = [];

/** 用锚点替换；必须恰好命中一次。 */
function applyAnchor(info, label, anchor, replacement) {
  const a = toEol(anchor, info.eol);
  const r = toEol(replacement, info.eol);
  const count = info.text.split(a).length - 1;
  if (count !== 1) {
    errors.push(`[${label}] 锚点命中 ${count} 次（要求恰好 1 次）：${anchor.split('\n')[0].slice(0, 70)}…`);
    return;
  }
  info.text = info.text.replace(a, r);
  notes.push(`[${label}] 锚点命中 1 次 ✅  (+${r.length - a.length} 字符)`);
}

/* ---------- 组装 ---------- */
const app = readFileInfo(targets.app);
const html = readFileInfo(targets.html);
const css = readFileInfo(targets.css);

notes.push(`app.js  换行=${app.crlf ? 'CRLF' : 'LF'} BOM=${app.bom}  ${app.bytes} 字节`);
notes.push(`index.html 换行=${html.crlf ? 'CRLF' : 'LF'} BOM=${html.bom}  ${html.bytes} 字节`);
notes.push(`style.css  换行=${css.crlf ? 'CRLF' : 'LF'} BOM=${css.bom}  ${css.bytes} 字节`);

// --- A1：世界状态条上显示当前用哪份存档 ---
applyAnchor(app, 'A1 状态条显示存档',
  `  const line = $('worldLine');
  line.textContent = wb.line || '（世界服务不可用）';`,
  `  const line = $('worldLine');
  // 顺便说清"当前用哪份存档"：跟随最新 / 已锁定某个 key
  line.textContent = (wb.line || '（世界服务不可用）') +
    (wb.saveKey ? \`  ·  存档 \${wb.saveKey}\` : '  ·  跟随最新存档');`);

// --- A2：插入存档选择模块 ---
applyAnchor(app, 'A2 插入存档模块',
  `/* ------------------------- 交互：房间设置 ------------------------- */`,
  `${fragApp.replace(/\s*$/, '')}

/* ------------------------- 交互：房间设置 ------------------------- */`);

// --- A3：打开房间面板时拉存档列表 ---
applyAnchor(app, 'A3 打开面板时拉列表',
  `    }).join('');
  }

  $('roomModal').hidden = false;`,
  `    }).join('');
    // 存档列表走独立接口（/api/state 刻意不带完整列表，只带数量）
    loadWbSaves();
  }

  $('roomModal').hidden = false;`);

// --- H1：房间设置里加存档选择区 ---
applyAnchor(html, 'H1 插入存档选择区',
  `        <button class="btn tiny ghost" id="r_autoAssign" style="margin-top:8px">自动按顺序分配</button>`,
  `        <button class="btn tiny ghost" id="r_autoAssign" style="margin-top:8px">自动按顺序分配</button>
${fragHtml.replace(/\s*$/, '')}`);

// --- C1：样式 ---
const cssBlock = `
/* ---- 世界存档选择器 ---- */
.save-picker select { width: 100%; }
.save-picker select:disabled { opacity: .6; cursor: not-allowed; }
.tip.save-stale { color: #ff9c9c; }
.tip .save-dir { color: var(--muted); font-size: 10.5px; word-break: break-all; }
`;
if (css.text.includes('.save-picker')) {
  notes.push('[C1 样式] 已存在 .save-picker，跳过（幂等）');
} else {
  const endsNl = css.text.endsWith('\n');
  css.text = css.text + toEol((endsNl ? '' : '\n') + cssBlock, css.eol);
  notes.push(`[C1 样式] 追加 ${cssBlock.length} 字符 ✅`);
}

/* ---------- 报告 ---------- */
console.log('=== 锚点检查 ===');
for (const n of notes) console.log('  ' + n);
if (errors.length) {
  console.log('\n=== 中止：锚点不满足，未写任何文件 ===');
  for (const e of errors) console.log('  ✗ ' + e);
  process.exit(2);
}

console.log('\n=== 预览 ===');
console.log(`  app.js    ${app.bytes} → ${Buffer.byteLength(app.text, 'utf8') + (app.bom ? 3 : 0)} 字节`);
console.log(`  index.html ${html.bytes} → ${Buffer.byteLength(html.text, 'utf8') + (html.bom ? 3 : 0)} 字节`);
console.log(`  style.css  ${css.bytes} → ${Buffer.byteLength(css.text, 'utf8') + (css.bom ? 3 : 0)} 字节`);

if (DRY) {
  console.log('\n--dry-run：只检查，不写文件。');
  process.exit(0);
}

/* ---------- 备份 + 写入 ---------- */
const backupDir = path.join(SPACE, '.backup-savepick');
fs.mkdirSync(backupDir, { recursive: true });
const stamp = String(Date.now());
const written = [];
for (const info of [app, html, css]) {
  const name = path.basename(info.path);
  fs.copyFileSync(info.path, path.join(backupDir, `${name}.${stamp}.bak`));
  const out = (info.bom ? '\ufeff' : '') + info.text;
  fs.writeFileSync(info.path, out, 'utf8');
  written.push(`${info.path}  ${fs.statSync(info.path).size} 字节`);
}

/* ---------- 写后自检 ---------- */
const checks = [
  [targets.app, 'loadWbSaves', true],
  [targets.app, 'switchWbSave', true],
  [targets.app, "r_wbSave", true],
  [targets.app, 'wbSaveField', true],
  [targets.html, 'id="r_wbSave"', true],
  [targets.html, 'id="r_wbSaveReload"', true],
  [targets.html, 'id="r_wbSaveMeta"', true],
  [targets.css, '.save-picker', true],
];
let bad = 0;
console.log('\n=== 写后自检 ===');
for (const [p, needle, want] of checks) {
  const has = fs.readFileSync(p, 'utf8').includes(needle);
  const ok = has === want;
  if (!ok) bad++;
  console.log(`  ${ok ? '✅' : '❌'} ${path.basename(p)} 含 ${needle} = ${has}`);
}
console.log('\n=== 已写入 ===');
for (const w of written) console.log('  ' + w);
console.log('备份目录: ' + backupDir);
if (bad) {
  console.log('\n⚠️ 自检有失败项，请用备份回滚：');
  for (const info of [app, html, css]) {
    console.log(`  Copy-Item "${path.join(backupDir, path.basename(info.path) + '.' + stamp + '.bak')}" "${info.path}" -Force`);
  }
  process.exit(3);
}
console.log('自检全部通过 ✅');
