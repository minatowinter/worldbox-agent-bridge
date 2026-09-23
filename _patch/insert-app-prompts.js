/* ------------------------- 人设：自动生成 + 手写 ------------------------- */

/*
 * 最终人设 = 手写(systemPrompt) + 自动(systemPromptAuto)，拼接后才是角色拿到的。
 * 自动那段带一个"生成时的世界指纹"，指纹变了说明换了存档/世界推进过，
 * 里面的国名和国王可能已经不存在了——所以必须让使用者看得见。
 */

/** 指纹形如 "C:\...\1786604527\map.wbax|1786604527"，取出末尾的存档 key。 */
function wbSaveKeyOfFingerprint(fp) {
  const s = String(fp || '');
  const bar = s.lastIndexOf('|');
  return bar >= 0 ? s.slice(bar + 1) : (s || null);
}

function wbPromptState(a) {
  const auto = String(a.systemPromptAuto || '').trim();
  const manual = String(a.systemPrompt || '').trim();
  const eff = String(a.effectivePrompt || '').trim();
  const wb = (S.active && S.active.worldbox) || null;
  const fp = wb && wb.status ? wb.status.fingerprint : null;
  const curKey = wbSaveKeyOfFingerprint(fp);
  const forKey = wbSaveKeyOfFingerprint(a.systemPromptAutoFor);
  const stale = !!(auto && a.systemPromptAutoFor && fp && a.systemPromptAutoFor !== fp);
  return { auto, manual, eff, stale, forKey, curKey };
}

/** 角色卡上的人设区（默认折叠，点开才看正文）。 */
function wbPromptBlock(a) {
  const wb = (S.active && S.active.worldbox) || null;
  if (!wb || !wb.enabled) return '';
  const p = wbPromptState(a);

  let cls = 'none';
  let text;
  if (!p.auto && !p.manual) {
    text = '人设：<b>无</b> —— 点世界栏的「🪄 重新生成人设」可按世界数据生成';
  } else if (p.auto && p.manual) {
    cls = p.stale ? 'warn' : 'good';
    text = '人设：手写 + 自动' + (p.stale
      ? `　⚠️ 自动那段是按存档 ${esc(p.forKey || '?')} 生成的，当前是 ${esc(p.curKey || '?')}`
      : '　✅ 与当前世界一致');
  } else if (p.auto) {
    cls = p.stale ? 'warn' : 'good';
    text = '人设：自动生成' + (p.stale
      ? `　⚠️ 按存档 ${esc(p.forKey || '?')} 生成，当前是 ${esc(p.curKey || '?')}`
      : `　✅ 与当前世界一致（存档 ${esc(p.curKey || '?')}）`);
  } else {
    text = '人设：<b>你手写的</b>（不会被自动覆盖）';
  }

  const blocks = [];
  if (p.auto) {
    blocks.push(`<details class="prompt-block"><summary>自动生成的人设（${p.auto.length} 字）</summary>` +
      `<pre class="prompt-pre">${esc(p.auto)}</pre></details>`);
  }
  if (p.manual) {
    blocks.push(`<details class="prompt-block"><summary>你手写的人设（${p.manual.length} 字）· 优先级最高</summary>` +
      `<pre class="prompt-pre">${esc(p.manual)}</pre></details>`);
  }
  if (p.eff) {
    blocks.push(`<details class="prompt-block"><summary>最终生效（手写 + 自动，${p.eff.length} 字）</summary>` +
      `<pre class="prompt-pre">${esc(p.eff)}</pre></details>`);
  }
  return `<div class="agent-prompt"><div class="prompt-line ${cls}">${text}</div>${blocks.join('')}</div>`;
}

/** 房间设置里的统计行：多少份自动、多少份手写、多少份已经过期。 */
function renderPromptStats() {
  const box = $('r_promptStats');
  if (!box || !S.active) return;
  const wb = S.active.worldbox || {};
  const agents = S.active.agents || [];
  let auto = 0; let manual = 0; let stale = 0;
  for (const a of agents) {
    const p = wbPromptState(a);
    if (p.auto) auto++;
    if (p.manual) manual++;
    if (p.stale) stale++;
  }
  box.innerHTML = `共 <b>${agents.length}</b> 位领袖：自动生成 <b>${auto}</b> · 手写 <b>${manual}</b>` +
    (stale ? ` · <span class="save-stale">⚠️ ${stale} 份是为旧存档生成的</span>` : '') +
    '　' + (wb.saveKey ? `当前存档 <b>${esc(wb.saveKey)}</b>` : '当前跟随最新存档');
}

/** 重新生成人设。force=false 时只补空白与世界已变的那些。 */
async function regenPrompts(force) {
  try {
    // 变量别叫 res —— 会遮蔽响应对象（这个项目踩过）
    const d = await api('/api/worldbox/prompts', { method: 'POST', body: { force: !!force } });
    const r = d.result || {};
    if (r.error) {
      toast('生成人设失败：' + r.error, 'err');
      return;
    }
    toast(`人设已更新：生成 ${r.filled || 0} · 重算 ${r.refreshed || 0} · 保留 ${r.skipped || 0}` +
      '（你手写的 systemPrompt 没有被改动）', 'ok');
    await loadState();
    if ($('roomModal') && !$('roomModal').hidden) openRoomModal();
  } catch (e) {
    toast('生成人设失败：' + e.message, 'err');
  }
}
