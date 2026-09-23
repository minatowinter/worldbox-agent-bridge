#!/usr/bin/env node
'use strict';

/**
 * 本地 Mock LLM —— 在没有真实 API Key 的情况下验证整条链路。
 * 模拟一个 OpenAI 兼容的 /v1/chat/completions 接口：
 *   · 判断类调用（"你需要发言吗"） → 前面的批次答 YES，后面的答 NO（用于测试按需发言的终止）
 *   · 普通发言调用 → 第一次返回工具调用，收到工具结果后返回最终发言
 *   · 世界房间 → 先查 world_state，再按 skill 格式产出一份《国策》
 *
 * 用法： node test/mock-llm.js [port]   (默认 8899)
 * 环境变量：
 *   MOCK_NATIVE=1   用模型「原生工具标记」的写法调用工具（而不是本项目的 tool 标签），
 *                   用来验证服务端对这种泄漏标记的兼容解析。
 */

const http = require('node:http');

const PORT = Number(process.argv[2] || 8899);
const NATIVE = !!process.env.MOCK_NATIVE;
let decisions = 0;

// 用字符拼出原生标记，源码里不直接出现完整形式
const BAR = '\uFF5C';
const D = 'D' + 'SML';
const open = (s) => '<' + BAR + BAR + D + BAR + BAR + s + '>';
const close = (s) => '<' + '/' + BAR + BAR + D + BAR + BAR + ' ' + s + '>';
function nativeCall(tool, params) {
  const body = Object.entries(params).map(([k, v]) => open(`parameter name="${k}"`) + '\n' + v + '\n' + close('parameter')).join('\n');
  return open(`invoke name="${tool}"`) + '\n' + body + '\n' + close('invoke');
}

const server = http.createServer((req, res) => {
  if (!req.url.includes('/chat/completions')) { res.writeHead(404).end('not found'); return; }
  let raw = '';
  req.on('data', (c) => (raw += c));
  req.on('end', () => {
    let body = {};
    try { body = JSON.parse(raw); } catch { /* ignore */ }
    const messages = body.messages || [];
    const sys = String((messages.find((m) => m.role === 'system') || {}).content || '');
    const last = messages[messages.length - 1] || {};
    const seenToolResult = messages.some((m) => String(m.content || '').includes('<tool_result'));

    const who = (sys.match(/你是：(.+)/) || [])[1]?.trim() || '某个角色';
    const readonly = /只读/.test(sys);
    if (/WorldBox/.test(sys)) {
      const cap = (sys.match(/本轮最多 (\d+) 条/) || [])[1];
      console.log(`[mock-llm] 世界上下文: 局势段=${/WorldBox 世界局势/.test(sys)} 裁决回执=${/创世者裁决回执/.test(sys)} 决策额度段=${/## 决策额度/.test(sys)}${cap ? `(上限${cap})` : ''} 体积=${Buffer.byteLength(sys, 'utf8')}B`);
    }

    let content;
    if (/你现在需要发言吗/.test(String(last.content || ''))) {
      decisions++;
      content = decisions <= 3 ? 'YES 我有别人没提到的补充' : 'NO 没什么要补充的了';
      console.log(`[mock-llm] 判断请求 #${decisions} → ${content}`);
    } else if (/WorldBox 世界局势/.test(sys)) {
      // WorldBox 房间：先看一眼世界状态工具，再按 skill 格式产出《国策》
      if (!seenToolResult) {
        content = '（先查一下我的国情）\n\n<tool name="world_state">\n{}\n</tool>';
      } else {
        const nation = (sys.match(/你是 WorldBox 世界「(.+?)」中「(.+?)」的领袖/) || [])[2] || who;
        content =
          `## 第 212 年 · ${nation}国策\n\n` +
          '### 零、情报状态\n存档已过期，仅供参考。\n\n' +
          '### 二、国策\n' +
          '**A. 落点型措施**\n' +
          '1. **补住房**\n   - 执行：请创世者在 北平(4,23) 增建/升级住房，容量 +83\n' +
          '2. **压饥饿**\n   - 执行：请创世者在 荆州(21,8) 投放食物约 25 人份\n' +
          '3. **修路**\n   - 执行：请创世者在 大辽(5,29) 修建道路，连接北平\n' +
          '4. **扩军**\n   - 执行：请创世者在 皖州(23,28) 征募士兵 20 人\n' +
          '5. **开矿**\n   - 执行：请创世者在 琼州(5,3) 开采矿石，补充国库\n' +
          '**B. 开关型措施**\n' +
          '6. **减税**\n   - 执行（全国开关）：把「地方高税」下调一档，试行一年\n\n' +
          '### 四、内心\n我不想再打下去了，先让百姓活下去。';
      }
    } else if (seenToolResult) {
      content = `（${who}）我已经把文件写进工作区了，@May 你看看。`;
    } else if (NATIVE) {
      // 用「原生工具标记」的写法，模拟 DeepSeek 系模型泄漏内部标记
      content = '（我先看一下工作区里有什么）\n\n' + nativeCall('list_files', {});
    } else {
      content =
        `（${who}）我先把方案落成一个文件。` + (readonly ? '（我以为我有写权限）' : '') + '\n\n' +
        '<tool name="write_file">\n' +
        JSON.stringify({
          path: `notes/${who}.md`,
          content: `# ${who} 的产出\n\n- 由 mock-llm 生成\n- 角色：${who}\n`,
        }) +
        '\n</tool>';
    }

    const out = JSON.stringify({
      id: 'chatcmpl-mock',
      object: 'chat.completion',
      model: body.model || 'mock',
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 120, completion_tokens: 40, total_tokens: 160 },
    });
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(out);
  });
});

server.listen(PORT, () => console.log(`[mock-llm] listening on http://127.0.0.1:${PORT}/v1`));
