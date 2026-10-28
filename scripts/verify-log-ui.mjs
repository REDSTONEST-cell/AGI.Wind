// Headless check for the system log drawer: Chinese labels and group colours.
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';

const BASE = process.env.BASE || 'http://127.0.0.1:18440';
const port = 9364;
const chrome = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--user-data-dir=${process.env.TEMP}/agiwind-logs-${Date.now()}`,
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

const lines = [
  '21:00:01 INFO [nagi.delivery] generation 12345678 running provider=deepseek model=deepseek-flash',
  '21:00:02 INFO [nagi] http POST /api/v1/chat/requests -> 202 70ms',
  '21:00:03 INFO [nagi.voice] voice synthesized voice_id=yuwan seconds=5.20 total_ms=3800 rtf=0.73 first_audio_ms=400',
  '21:00:04 INFO [nagi.memory] memory extracted companion=abcdef events=8 candidates=4 stored=1 reinforced=0 quarantined=0 ignored=0 calls=1',
  '21:00:05 ERROR [nagi.delivery] generation 12345678 failed code=GENERATION_FAILED',
  '21:00:06 INFO [nagi] startup complete',
];
const mock = `(() => {
  const original = window.fetch.bind(window);
  let served = false;
  const json = (value) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });
  window.fetch = async (input, init = {}) => {
    const url = String(input && input.url ? input.url : input);
    if (url.includes('/api/v1/system-logs')) {
      if (served) return json({ lines: [], offsets: { stdout: 0, stderr: 0 }, reset: false });
      served = true;
      return json({ lines: ${JSON.stringify(lines)}.map((text) => ({ stream: 'stderr', text })), offsets: { stdout: 0, stderr: 0 }, reset: false });
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
  await until(`!!document.getElementById('log-toggle')`);
  await sleep(800);
  await evaluate(`document.getElementById('log-toggle').click()`);
  await until(`document.querySelectorAll('#term-output .term-entry').length >= 5`);
  const rows = await evaluate(`([...document.querySelectorAll('#term-output .term-entry')].map((entry) => ({
    tag: entry.querySelector('.term-tag')?.textContent || '',
    text: entry.querySelector('.term-text')?.textContent || '',
    group: entry.dataset.group || '',
    level: entry.dataset.level || '',
    color: getComputedStyle(entry.querySelector('.term-tag')).color,
  })))`);
  const byGroup = {};
  for (const row of rows) if (!byGroup[row.group]) byGroup[row.group] = row;
  for (const group of ['gen', 'api', 'voice', 'memory', 'sys']) assert(byGroup[group], 'missing group ' + group);
  console.log('rows', JSON.stringify(rows));
  assert(byGroup.gen.tag === '生成' && byGroup.gen.text.includes('开始生成'));
  assert(byGroup.api.tag === '接口' && byGroup.api.text.includes('接口 POST'));
  assert(byGroup.voice.tag === '语音' && byGroup.voice.text.includes('语音合成完成'));
  assert(byGroup.memory.tag === '记忆' && byGroup.memory.text.includes('记忆提炼'));
  assert(byGroup.gen.level === 'error' || rows.some((row) => row.level === 'error'));
  const colors = new Set(rows.map((row) => row.color));
  assert(colors.size >= 4, 'log groups should use different colours: ' + [...colors].join(','));
  await evaluate(`document.querySelector('.log-filter[data-filter="voice"]').click()`);
  await until(`document.querySelectorAll('#term-output .term-entry').length === 1`);
  const filtered = await evaluate(`document.querySelector('#term-output .term-entry')?.dataset.group`);
  assert.equal(filtered, 'voice');
  assert.equal(errors.length, 0, JSON.stringify(errors).slice(0, 500));
  console.log('Log drawer: PASS (Chinese labels, translated text, group colours, voice filter)');
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
}
