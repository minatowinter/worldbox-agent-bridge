/*
 * 补丁 4：把"你手写的人设"改成如实的说法。
 *
 * 原因：leaderAgent()（server.js:151-158）在建房间时就给每位领袖写了一段默认 systemPrompt，
 * 所以把 systemPrompt 一律叫"你手写的"是错的；而且它非空会让
 * autofillSystemPrompts 的「世界已变→重算」分支永远走不到（实测 force:false 全部跳过）。
 *
 * 用法：node _patch/patch_prompts_wording.js [--dry-run]
 */
const fs = require('fs');
const path = require('path');
const SPACE = 'C:/Users/24771/Desktop/space';
const DRY = process.argv.includes('--dry-run');

const targets = { app: path.join(SPACE, 'public/app.js'), html: path.join(SPACE, 'public/index.html') };

function info(p) {
  let text = fs.readFileSync(p, 'utf8');
  const bom = text.charCodeAt(0) === 0xfeff;
  if (bom) text = text.slice(1);
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  return { path: p, text, bom, eol, bytes: fs.statSync(p).size };
}
const toEol = (s, eol) => (eol === '\r\n' ? s.replace(/\r\n/g, '\n').replace(/\n/g, '\r\n') : s.replace(/\r\n/g, '\n'));

const app = info(targets.app);
const html = info(targets.html);
const errors = [];
const notes = [];

function sub(f, label, from, to) {
  const a = toEol(from, f.eol);
  const b = toEol(to, f.eol);
  const n = f.text.split(a).length - 1;
  if (n !== 1) { errors.push(`[${label}] 命中 ${n} 次（要求 1）`); return; }
  f.text = f.text.replace(a, b);
  notes.push(`[${label}] ✅`);
}

sub(app, 'W1 手写+自动 的措辞',
  `    text = '人设：手写 + 自动' + (p.stale`,
  `    text = '人设：既有设定 + 自动生成' + (p.stale`);

sub(app, 'W2 只有 systemPrompt 时的措辞',
  `    text = '人设：<b>你手写的</b>（不会被自动覆盖）';`,
  `    text = '人设：<b>既有 systemPrompt</b>（自动生成不会覆盖它）';`);

sub(app, 'W3 折叠块标题',
  `<summary>你手写的人设（\${p.manual.length} 字）· 优先级最高</summary>`,
  `<summary>既有 systemPrompt（\${p.manual.length} 字）· 不会被自动覆盖</summary>`);

sub(html, 'W4 设置面板说明',
  `          <b>你自己写的人设永远不会被覆盖</b>：自动生成的内容单独放在 <code>systemPromptAuto</code> 里，
          两者拼接后才是角色真正拿到的人设。角色卡上能分别看到它们。
          <br>「重新生成全部」会按当前世界重算自动那段（手写的不动）；「只补空缺与过期」只处理还没有人设、或人设对应的存档已经变了的角色。`,
  `          <b>角色的 <code>systemPrompt</code> 永远不会被覆盖</b>：自动生成的内容单独放在 <code>systemPromptAuto</code> 里，
          两者拼接后才是角色真正拿到的人设。角色卡上能分别看到它们，也能看到自动那段是按哪份存档生成的。
          <br>「重新生成全部」按当前世界重算自动那段（<code>systemPrompt</code> 不动）；「只补空缺与过期」只处理还没有自动人设的角色。
          <br>⚠️ 建房间时服务端已经给每位领袖写了一段默认 <code>systemPrompt</code>，它同样受保护——
          所以「只补空缺与过期」在这种房间里通常不会做事（实测 20/20 全部跳过）。要按世界数据重算，请用「重新生成全部人设」。`);

console.log('=== 锚点 ===');
for (const n of notes) console.log('  ' + n);
for (const f of [app, html]) console.log(`  ${path.basename(f.path)} ${f.bytes} → ${Buffer.byteLength(f.text, 'utf8') + (f.bom ? 3 : 0)}`);
if (errors.length) { console.log('\n中止：'); for (const e of errors) console.log('  ✗ ' + e); process.exit(2); }
if (DRY) { console.log('\n--dry-run：只检查。'); process.exit(0); }

const backupDir = path.join(SPACE, '.backup-prompts');
fs.mkdirSync(backupDir, { recursive: true });
const stamp = String(Date.now());
for (const f of [app, html]) {
  fs.copyFileSync(f.path, path.join(backupDir, `${path.basename(f.path)}.${stamp}.bak`));
  fs.writeFileSync(f.path, (f.bom ? '\ufeff' : '') + f.text, 'utf8');
}
console.log('\n=== 已写入 ===');
for (const f of [app, html]) console.log(`  ${f.path}  ${fs.statSync(f.path).size} 字节`);
// 写后自检：旧措辞不应再出现
const bad = [];
for (const [p, needle] of [[app.path, '你手写的'], [html.path, '你自己写的人设永远不会被覆盖']]) {
  if (fs.readFileSync(p, 'utf8').includes(needle)) bad.push(`${path.basename(p)} 仍含 ${needle}`);
}
console.log(bad.length ? '⚠️ ' + bad.join(' / ') : '自检通过：旧措辞已消失 ✅');
process.exit(bad.length ? 3 : 0);
