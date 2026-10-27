// Headless check for proactive messages: live append + settings form.
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';

const BASE = process.env.BASE || 'http://127.0.0.1:18440';
const port = 9363;
const chrome = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--user-data-dir=${process.env.TEMP}/agiwind-proactive-${Date.now()}`,
  `--remote-debugging-port=${port}`, 'about:blank',
], { stdio: 'ignore', windowsHide: true });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let ws, seq = 0; const pending = new Map(); const errors = [];
const send = (method, params = {}) => { const id = ++seq; return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(Error('timeout ' + method)); } }, 15000); }); };
const evaluate = async (expression) => {
  const value = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (value.exceptionDetails) throw Error(JSON.stringify(value.exceptionDetails));
  return value.result.value;
};
const until = async (expression, tries = 120) => { for (let i = 0; i < tries; i++) { try { if (await evaluate(expression)) return; } catch {} await sleep(100); } throw Error('condition failed: ' + expression); };

const mock = `(() => {
  const original = window.fetch.bind(window);
  const state = window.__proactiveMock = { patches: [], sends: 0, liveConnections: 0, cursorQueries: 0, afters: [] };
  const json = (value) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });
  const config = { enabled: true, min_idle_minutes: 45, min_gap_minutes: 90, max_per_day: 2, quiet_start: '23:00', quiet_end: '08:00', sent_today: 1, last_sent_at: new Date().toISOString(), due: false, reason: 'not_idle', continuation_enabled: true, continuation_max_rounds: 6, continuation_max_messages: 3, continuation_max_wait_seconds: 900, continuation_lifetime_minutes: 30 };
  window.fetch = async (input, init = {}) => {
    const url = String(input && input.url ? input.url : input);
    if (url.includes('/api/v1/proactive/settings')) {
      if (init.method === 'PATCH') {
        const body = JSON.parse(init.body || '{}');
        state.patches.push(body);
        Object.assign(config, body);
        return json(config);
      }
      return json(config);
    }
    if (url.includes('/api/v1/proactive/send')) {
      state.sends += 1;
      return json({ sent: true, event_id: 'manual-proactive-1', text: '测试主动消息' });
    }
    if (url.includes('/api/v1/conversations/') && url.endsWith('/live-cursor')) {
      state.cursorQueries += 1;
      return json({sequence:10});
    }
    if (url.includes('/api/v1/conversations/') && url.includes('/live?')) {
      state.liveConnections += 1;
      state.afters.push(Number((url.match(/after=(\\d+)/)||[])[1]||0));
      let sent = false;
      const body = new ReadableStream({
        start(controller) {
          state.closeLive = () => controller.close();
          const encoder = new TextEncoder();
          const emit = () => {
            if (sent) return;
            sent = true;
            const payload = {
              event_id: 'proactive-mock-1',
              conversation_id: 'mock',
              text: '我刚刚看到一只很圆的猫。',
              recorded_at: new Date().toISOString(),
              trigger: 'schedule',
            };
            const old = {sequence:9,payload:{run_id:'historical-no-work',decision:'silent',reason:'daily_cap',rounds:0,steps:0}};
            controller.enqueue(encoder.encode('id: 9\\nevent: idle_cognition.completed\\ndata: ' + JSON.stringify(old) + '\\n\\n'));
            const row = { id: '11', sequence: 11, type: 'proactive.message', payload };
            controller.enqueue(encoder.encode('id: 11\\nevent: proactive.message\\ndata: ' + JSON.stringify(row) + '\\n\\n'));
            const waiting = {sequence:12,payload:{run_id:'same-task',seconds:300,rounds:2,steps:2,sent_count:1}};
            controller.enqueue(encoder.encode('id: 12\\nevent: idle_cognition.waiting\\ndata: ' + JSON.stringify(waiting) + '\\n\\n'));
            const skipped = {sequence:13,payload:{run_id:'fresh-no-work',decision:'silent',reason:'user_message',rounds:0,steps:0}};
            controller.enqueue(encoder.encode('id: 13\\nevent: idle_cognition.completed\\ndata: ' + JSON.stringify(skipped) + '\\n\\n'));
          };
          setTimeout(emit, 900);
          setTimeout(() => { try { controller.close(); } catch {} }, 60000);
        },
      });
      return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }
    return original(input, init);
  };
})();`;

try {
  for (let i = 0; i < 100; i++) { try { if ((await fetch(`http://127.0.0.1:${port}/json/version`)).ok) break; } catch {} await sleep(100); }
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.onmessage = (event) => {
    const value = JSON.parse(event.data);
    if (value.id && pending.has(value.id)) { const entry = pending.get(value.id); pending.delete(value.id); value.error ? entry.reject(Error(JSON.stringify(value.error))) : entry.resolve(value.result); }
    else if (value.method === 'Runtime.exceptionThrown') errors.push(value.params.exceptionDetails);
  };
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: mock });
  await send('Page.navigate', { url: BASE + '/#/chat' });
  await until(`!!document.querySelector('#messages')`);
  await sleep(1200);
  await until(`document.getElementById('messages').innerText.includes('我刚刚看到一只很圆的猫')`, 120);
  const appended = await evaluate(`(() => { const nodes=[...document.querySelectorAll('#messages .msg[data-role="assistant"]')]; const last=nodes[nodes.length-1]; return { text: last?.innerText||'', liveConnections: window.__proactiveMock.liveConnections }; })()`);
  assert.match(appended.text, /我刚刚看到一只很圆的猫/);
  assert(appended.liveConnections >= 1);
  assert.equal(await evaluate('window.__proactiveMock.cursorQueries'),1);
  await until(`document.body.innerText.includes('任务暂歇')`);
  await evaluate(`document.getElementById('log-toggle').click()`);
  await evaluate(`(() => {const b=document.getElementById('log-mode');if(b.textContent.includes('原始'))b.click();})()`);
  await evaluate(`window.__proactiveMock.closeLive()`);
  await until(`window.__proactiveMock.liveConnections >= 2`);
  await sleep(1100);
  const replay = await evaluate(`({afters:window.__proactiveMock.afters,waits:[...document.querySelectorAll('#term-output .term-entry')].filter(n=>n.innerText.includes('同一任务已保存')).length,text:document.getElementById('term-output').innerText})`);
  assert.deepEqual(replay.afters.slice(0,2),[10,13]);
  assert.equal(replay.waits,1);
  assert(!replay.text.includes('historical-no-work')&&!replay.text.includes('fresh-no-work'));
  await send('Page.navigate', { url: BASE + '/#/model' });
  await until(`!!document.getElementById('proactive-settings-form')`);
  await sleep(800);
  const form = await evaluate(`({ enabled: document.getElementById('proactive-enabled').checked, idle: document.getElementById('proactive-idle').value, gap: document.getElementById('proactive-gap').value, max: document.getElementById('proactive-max').value, quiet: document.getElementById('proactive-quiet-start').value, state: document.getElementById('proactive-state').textContent })`);
  assert.equal(form.enabled, true);
  assert.equal(form.idle, '45');
  assert.equal(form.gap, '90');
  assert.equal(form.max, '2');
  assert.equal(form.quiet, '23:00');
  await evaluate(`(() => { document.getElementById('proactive-idle').value='60'; document.getElementById('proactive-settings-form').requestSubmit(); return true; })()`);
  await until(`window.__proactiveMock.patches.length === 1`);
  const saved = await evaluate(`window.__proactiveMock.patches`);
  assert.equal(saved.length, 1);
  assert.equal(saved[0].min_idle_minutes, 60);
  assert.equal(saved[0].continuation_enabled, true);
  assert.equal(saved[0].continuation_max_rounds, 6);
  assert.equal(saved[0].continuation_max_messages, 3);
  assert.equal(saved[0].continuation_max_wait_seconds, 900);
  assert.equal(saved[0].continuation_lifetime_minutes, 30);
  await evaluate(`document.getElementById('proactive-test').click()`);
  await until(`window.__proactiveMock.sends === 1`);
  assert.equal(errors.length, 0, JSON.stringify(errors).slice(0, 500));
  console.log('Proactive client: PASS (snapshot cursor, reconnect dedupe, zero-work filtering, live messages, settings, zero JS errors)');
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
}
