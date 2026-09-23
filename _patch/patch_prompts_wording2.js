/*
 * 补丁 5：清掉人设模块里剩下的"手写"措辞（这些 systemPrompt 多数是服务端默认生成的）。
 * 用法：node _patch/patch_prompts_wording2.js [--dry-run]
 */
const fs = require('fs');
const path = require('path');
const APP = 'C:/Users/24771/Desktop/space/public/app.js';
const DRY = process.argv.includes('--dry-run');

let text = fs.readFileSync(APP, 'utf8');
const bom = text.charCodeAt(0) === 0xfeff;
if (bom) text = text.slice(1);
const eol = text.includes('\r\n') ? '\r\n' : '\n';
const toEol = (s) => (eol === '\r\n' ? s.replace(/\r\n/g, '\n').replace(/\n/g, '\r\n') : s.replace(/\r\n/g, '\n'));

const subs = [
  ['/* ------------------------- 人设：自动生成 + 手写 ------------------------- */',
   '/* ------------------------- 人设：自动生成 + 既有 systemPrompt ------------------------- */'],
  [' * 最终人设 = 手写(systemPrompt) + 自动(systemPromptAuto)，拼接后才是角色拿到的。',
   ' * 最终人设 = 既有 systemPrompt + 自动(systemPromptAuto)，拼接后才是角色拿到的。'],
  ['<summary>最终生效（手写 + 自动，${p.eff.length} 字）</summary>',
   '<summary>最终生效（systemPrompt + 自动生成，${p.eff.length} 字）</summary>'],
  ['/** 房间设置里的统计行：多少份自动、多少份手写、多少份已经过期。 */',
   '/** 房间设置里的统计行：多少份自动、多少份已有 systemPrompt、多少份已过期。 */'],
  ['box.innerHTML = `共 <b>${agents.length}</b> 位领袖：自动生成 <b>${auto}</b> · 手写 <b>${manual}</b>`',
   'box.innerHTML = `共 <b>${agents.length}</b> 位领袖：自动生成 <b>${auto}</b> · 已有 systemPrompt <b>${manual}</b>`'],
  ["'（你手写的 systemPrompt 没有被改动）', 'ok');",
   "'（角色的 systemPrompt 没有被改动）', 'ok');"],
];

const errors = [];
for (const [from, to] of subs) {
  const n = text.split(toEol(from)).length - 1;
  if (n !== 1) { errors.push(`命中 ${n} 次: ${from.slice(0, 50)}…`); continue; }
  text = text.replace(toEol(from), toEol(to));
}
console.log(`=== ${subs.length - errors.length}/${subs.length} 处替换 ===`);
for (const e of errors) console.log('  ✗ ' + e);
if (errors.length) { console.log('中止，未写。'); process.exit(2); }
console.log(`  ${fs.statSync(APP).size} → ${Buffer.byteLength(text, 'utf8') + (bom ? 3 : 0)} 字节`);
if (DRY) { console.log('--dry-run：只检查。'); process.exit(0); }

const backupDir = 'C:/Users/24771/Desktop/space/.backup-prompts';
fs.mkdirSync(backupDir, { recursive: true });
const stamp = String(Date.now());
fs.copyFileSync(APP, path.join(backupDir, `app.js.${stamp}.bak`));
fs.writeFileSync(APP, (bom ? '\ufeff' : '') + text, 'utf8');
const left = ['你手写'].filter((n) => fs.readFileSync(APP, 'utf8').includes(n));
console.log(`已写入 ${APP}`);
console.log(left.length ? '⚠️ 仍含: ' + left.join(',') : '自检通过：不再有"你手写" ✅');
process.exit(left.length ? 3 : 0);
