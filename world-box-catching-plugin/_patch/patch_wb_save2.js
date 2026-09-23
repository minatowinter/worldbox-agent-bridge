/*
 * 补丁 2：修正存档选择器的三处联调问题。
 *   1) /api/v1/saves 的「当前选择」是对象 {requested, using, mode, matches}，不是字符串
 *   2) mode 不可信（数据服务启动时读到的持久化锁定会被报成「跟随最新存档」），只认 using
 *   3) 「数据服务真正在读的那份」用 ✅ 标出来——它和「本房间锁定了哪份」是两件事
 *
 * 用法：node _patch/patch_wb_save2.js [--dry-run]
 */
const fs = require('fs');
const path = require('path');

const APP = 'C:/Users/24771/Desktop/space/public/app.js';
const DRY = process.argv.includes('--dry-run');

let text = fs.readFileSync(APP, 'utf8');
const bytes0 = fs.statSync(APP).size;
const crlf = text.includes('\r\n');
const eol = crlf ? '\r\n' : '\n';
const toEol = (s) => (eol === '\r\n' ? s.replace(/\r\n/g, '\n').replace(/\n/g, '\r\n') : s.replace(/\r\n/g, '\n'));

const errors = [];
const notes = [];

function apply(label, anchor, replacement) {
  const a = toEol(anchor);
  const r = toEol(replacement);
  const n = text.split(a).length - 1;
  if (n !== 1) {
    errors.push(`[${label}] 锚点命中 ${n} 次（要求 1 次）`);
    return;
  }
  text = text.replace(a, r);
  notes.push(`[${label}] ✅ (+${r.length - a.length})`);
}

apply('F1 取 using 而不是整个对象',
  `    const cur = rows.find((s) => String(wbSaveField(s, 'key')) === String(locked)) || null;
    const using = d.selection;                 // 数据服务实际读的那份（Python 侧状态）`,
  `    const cur = rows.find((s) => String(wbSaveField(s, 'key')) === String(locked)) || null;
    // /api/v1/saves 的「当前选择」是一个对象 {requested, using, mode, matches}，不是字符串。
    // 这里只取 using：mode 不可信——数据服务启动时读到的持久化锁定会被报成「跟随最新存档」
    // （见 docs/多国演练发现-1国到20国.md 的 P4）。前端不显示 mode。
    const selRaw = d.selection;
    const using = (selRaw && typeof selRaw === 'object') ? (selRaw.using || null) : (selRaw || null);
    const effective = locked || using || null;`);

apply('F2 元信息说清"本房间"与"当前生效"',
  `    const bits = [];
    bits.push('本房间：' + (locked ? '已锁定 <b>' + esc(locked) + '</b>' : '跟随最新存档'));
    if (using && String(using) !== String(locked || '')) {
      bits.push('数据服务实际在用 <b>' + esc(using) + '</b>');
    }`,
  `    const bits = [];
    bits.push('本房间：' + (locked ? '已锁定 <b>' + esc(locked) + '</b>' : '未锁定，跟随最新'));
    if (effective) {
      bits.push('当前生效 <b>' + esc(effective) + '</b>' + (using && String(using) !== String(locked || '') ? '（由数据服务的存档选择决定）' : ''));
    }`);

apply('F3 标出数据服务真正在读的那份',
  `        const mark = wbSaveField(s, 'stale') ? '⚠️ ' : '';
        const use = String(key) === String(locked) ? ' selected' : '';
        html.push(\`<option value="\${esc(key)}"\${use}>\${mark}\${esc(wbSaveLabel(s, { noWorld: true }))}</option>\`);`,
  `        const stale = wbSaveField(s, 'stale') ? '⚠️ ' : '';
        const active = wbSaveField(s, 'selected') ? '✅ ' : '';
        const use = String(key) === String(locked) ? ' selected' : '';
        html.push(\`<option value="\${esc(key)}"\${use}>\${active}\${stale}\${esc(wbSaveLabel(s, { noWorld: true }))}</option>\`);`);

console.log('=== 补丁 2 锚点 ===');
for (const n of notes) console.log('  ' + n);
console.log(`  app.js 换行=${crlf ? 'CRLF' : 'LF'}  ${bytes0} → ${Buffer.byteLength(text, 'utf8')} 字节`);
if (errors.length) {
  console.log('\n中止，未写：');
  for (const e of errors) console.log('  ✗ ' + e);
  process.exit(2);
}
if (DRY) { console.log('\n--dry-run：只检查，不写。'); process.exit(0); }

const backupDir = 'C:/Users/24771/Desktop/space/.backup-savepick';
fs.mkdirSync(backupDir, { recursive: true });
const stamp = String(Date.now());
fs.copyFileSync(APP, path.join(backupDir, `app.js.${stamp}.bak`));
fs.writeFileSync(APP, text, 'utf8');
console.log('\n=== 已写入 ===');
console.log(`  ${APP}  ${fs.statSync(APP).size} 字节`);
console.log(`  备份: ${path.join(backupDir, `app.js.${stamp}.bak`)}`);
