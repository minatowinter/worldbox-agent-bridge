/* ------------------------- 交互：世界存档选择 ------------------------- */

/*
 * 存档列表有两个来源，字段拼写**不一样**，这里两种都认：
 *   GET /api/worldbox/saves     → Python 原始行：key / index / kind_label / world_name /
 *                                 time_local / age_text / stale / has_stats / size_mb
 *   GET /api/state 的 worldbox  → 已映射：world / age / sizeMb / hasStats …
 * 只认一种的话，换个来源整列就会空白（本项目有四处白名单序列化，吃过这个亏）。
 */
function wbSaveField(s, ...names) {
  for (const n of names) {
    if (s && s[n] != null && s[n] !== '') return s[n];
  }
  return null;
}

function wbIsRunning() {
  return !!(S.running && (S.runningRoomId == null || S.runningRoomId === (S.active && S.active.id)));
}

/** 一个存档在下拉框里的一行说明。 */
function wbSaveLabel(s, opts = {}) {
  const bits = [];
  if (!opts.noIndex) {
    const idx = wbSaveField(s, 'index');
    if (idx != null) bits.push('#' + idx);
  }
  if (!opts.noWorld) {
    const w = wbSaveField(s, 'world', 'world_name');
    if (w) bits.push(String(w));
  }
  bits.push(String(wbSaveField(s, 'kind', 'kind_label') || '存档'));
  const t = wbSaveField(s, 'time', 'time_local');
  if (t) bits.push(String(t));
  const age = wbSaveField(s, 'age', 'age_text');
  if (age) bits.push('距今 ' + age);
  const mb = wbSaveField(s, 'sizeMb', 'size_mb');
  if (mb != null) bits.push(mb + ' MB');
  if (wbSaveField(s, 'hasStats', 'has_stats') === false) bits.push('无统计库');
  return bits.join(' · ');
}

/** 读取存档列表并填进下拉框。列表按世界分组——同一个世界常有十几份自动存档。 */
async function loadWbSaves() {
  const sel = $('r_wbSave');
  const meta = $('r_wbSaveMeta');
  if (!sel || !meta) return;
  sel.disabled = true;
  sel.innerHTML = '<option value="">（正在读取存档列表…）</option>';
  try {
    const r = await fetch('/api/worldbox/saves');
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));

    const rows = d.saves || [];
    const locked = d.locked || '';
    const groups = new Map();
    for (const s of rows) {
      const w = String(wbSaveField(s, 'world', 'world_name') || '（未知世界）');
      if (!groups.has(w)) groups.set(w, []);
      groups.get(w).push(s);
    }

    const html = ['<option value="">跟随最新存档（不锁定）</option>'];
    for (const [world, list] of groups) {
      html.push(`<optgroup label="${esc(world)}（${list.length} 份）">`);
      for (const s of list) {
        const key = wbSaveField(s, 'key');
        // <option> 里没法上色，过期的只能用 ⚠️ 标出来
        const mark = wbSaveField(s, 'stale') ? '⚠️ ' : '';
        const use = String(key) === String(locked) ? ' selected' : '';
        html.push(`<option value="${esc(key)}"${use}>${mark}${esc(wbSaveLabel(s, { noWorld: true }))}</option>`);
      }
      html.push('</optgroup>');
    }
    sel.innerHTML = html.join('');
    sel.value = locked;

    const cur = rows.find((s) => String(wbSaveField(s, 'key')) === String(locked)) || null;
    const using = d.selection;                 // 数据服务实际读的那份（Python 侧状态）
    const staleNow = cur ? !!wbSaveField(cur, 'stale')
      : !!(S.active && S.active.worldbox && S.active.worldbox.status && S.active.worldbox.status.stale);

    const bits = [];
    bits.push('本房间：' + (locked ? '已锁定 <b>' + esc(locked) + '</b>' : '跟随最新存档'));
    if (using && String(using) !== String(locked || '')) {
      bits.push('数据服务实际在用 <b>' + esc(using) + '</b>');
    }
    bits.push('共 ' + esc(d.count != null ? d.count : rows.length) + ' 份存档');
    if (staleNow) bits.push('⚠️ 当前这份已过期，游戏可能没开，世界局势多半已经变了');
    meta.innerHTML = bits.join('　·　');
    meta.className = 'tip' + (staleNow ? ' save-stale' : '');
    if (d.dataDir) meta.innerHTML += `<br><span class="save-dir">数据目录：${esc(d.dataDir)}</span>`;
    if (d.error) meta.innerHTML += `<br><span class="save-stale">读取存档列表出错：${esc(d.error)}</span>`;
    if (wbIsRunning()) meta.innerHTML += '<br><span class="save-stale">房间正在运行，先停止才能切换存档。</span>';

    sel.disabled = wbIsRunning();
  } catch (e) {
    sel.innerHTML = '<option value="">（读不到存档列表）</option>';
    sel.disabled = true;
    meta.className = 'tip save-stale';
    meta.textContent = '读不到存档列表：' + e.message +
      '。请确认 python worldbox_agent.py serve 正在运行（默认 http://127.0.0.1:8777）。';
  }
}

/** 切换存档。成功后按新世界重建领袖，所以整个面板要重画。 */
async function switchWbSave(key) {
  const sel = $('r_wbSave');
  const meta = $('r_wbSaveMeta');
  if (!sel || !meta) return;
  sel.disabled = true;
  meta.className = 'tip';
  meta.textContent = '正在切换存档…（重新读取世界，并按新世界重建领袖）';
  try {
    // 注意：变量不要叫 res —— 会遮蔽别处的响应对象（这个项目踩过）
    const r = await fetch('/api/worldbox/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
    const d = await r.json().catch(() => ({}));

    if (r.status === 409) {
      toast('房间正在运行，请先停止再切换存档', 'err');
      meta.className = 'tip save-stale';
      meta.textContent = '房间正在运行，不接受切换存档。请先点「停止」，再回来选存档。';
      return;
    }
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));

    await loadState();
    const st = d.worldbox && d.worldbox.status;
    toast('已切换到 ' + (d.switched || (key || '最新存档')) +
      (st ? `（${st.worldName} 第 ${st.year} 年，存活 ${st.liveKingdoms} 国）` : '') +
      (d.rebuilt ? `，领袖重建 ${d.rebuilt} 位` : ''), 'ok');
    if (d.warning) toast('⚠️ ' + d.warning, 'err');
    openRoomModal();            // 领袖名单变了，重画面板（会重新拉存档列表）
  } catch (e) {
    toast('切换存档失败：' + e.message, 'err');
    meta.className = 'tip save-stale';
    meta.textContent = '切换失败：' + e.message;
    sel.disabled = wbIsRunning();
  }
}

{
  const sel = $('r_wbSave');
  if (sel) {
    sel.onchange = (e) => switchWbSave(e.target.value);
    $('r_wbSaveReload').onclick = () => loadWbSaves();
  }
}
