// Headless check for the usage page: real counts render into the stat cards.
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const BASE = process.env.BASE || 'http://127.0.0.1:18440';
const port = 9372;
const chrome = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--user-data-dir=${process.env.TEMP}/agiwind-usage-${Date.now()}`,
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
const until = async (expression, tries = 150) => { for (let i = 0; i < tries; i++) { try { if (await evaluate(expression)) return; } catch {} await sleep(100); } throw Error('condition failed: ' + expression); };

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
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: BASE + '/#/usage' });
  await until(`!document.getElementById('app').hidden && document.querySelectorAll('.usage-metric').length === 9`);
  await until(`document.getElementById('usage-messages').textContent !== '–'`, 200);
  await until(`document.querySelectorAll('#usage-call-chart svg rect.usage-bar').length === 24`, 200);
  await sleep(600);
  const cards = await evaluate(`([...document.querySelectorAll('.usage-metric')].map((card) => ({
    label: card.querySelector('.usage-label').textContent.trim(),
    value: card.querySelector('strong').textContent.trim(),
    hint: card.querySelector('small').textContent.trim(),
  })))`);
  console.log('cards', JSON.stringify(cards));
  assert.equal(cards.length, 9);
  for (const card of cards) assert(card.value && card.value !== '–', 'unfilled card ' + card.label);
  const labels = cards.map((card) => card.label);
  for (const wanted of ['消息总数', '今日模型调用', '今日 Token', '累计 Token', '语音消息', '工具调用', 'CPU', '进程内存', '运行时间']) {
    assert(labels.includes(wanted), 'missing card ' + wanted);
  }
  const numeric = (label) => Number(cards.find((card) => card.label === label).value.replace(/[^0-9.]/g, ''));
  assert(numeric('消息总数') > 0);
  assert(numeric('语音消息') > 0);
  assert(numeric('工具调用') > 0);
  assert(numeric('CPU') >= 0);
  assert(/MB|KB|B/.test(cards.find((card) => card.label === '进程内存').value), 'memory card should show a byte size');
  assert(/(h|m|s)/.test(cards.find((card) => card.label === '运行时间').value), 'uptime card should show a duration');

  const dashboard = await evaluate(`({
    line: !!document.querySelector('#usage-message-chart svg path.usage-line'),
    area: !!document.querySelector('#usage-message-chart svg path.usage-area'),
    bars: document.querySelectorAll('#usage-call-chart svg rect.usage-bar').length,
    toolRows: document.querySelectorAll('#usage-tool-rank .usage-rank-row').length,
    modelRows: document.querySelectorAll('#usage-model-rank .usage-rank-row').length,
    trendTotal: document.getElementById('usage-trend-total').textContent,
    windowTokens: document.getElementById('usage-window-tokens').textContent,
    calls: document.getElementById('usage-window-calls').textContent,
    ttft: document.getElementById('usage-ttft').textContent,
    response: document.getElementById('usage-response').textContent,
    tpm: document.getElementById('usage-tpm').textContent,
    success: document.getElementById('usage-success').textContent,
    notch: getComputedStyle(document.querySelector('.usage-metric'), '::after').content,
  })`);
  console.log('dashboard', JSON.stringify(dashboard));
  assert(dashboard.line && dashboard.area, 'message trend should draw a line and area');
  assert.equal(dashboard.bars, 24, 'model trend should draw 24 bars');
  assert(dashboard.toolRows >= 1, 'tool ranking should list at least one tool');
  assert(dashboard.modelRows >= 1, 'model ranking should list at least one model');
  assert(dashboard.trendTotal && dashboard.trendTotal !== '–');
  assert(dashboard.windowTokens && dashboard.windowTokens !== '–');
  assert(/次调用/.test(dashboard.calls));
  assert(dashboard.success !== '–');
  assert.equal(dashboard.notch, 'none', 'stat cards must not render a decorative notch');

  const layout = await evaluate(`({ display: getComputedStyle(document.querySelector('.usage-metrics')).display, cardWidth: document.querySelector('.usage-metric').getBoundingClientRect().width, radius: getComputedStyle(document.querySelector('.usage-metric')).borderRadius, heading: document.querySelector('[data-page="usage"] h1').textContent, nav: document.querySelector('.nav-item[data-page="usage"]').classList.contains('active') })`);
  assert(layout.display === 'flex', 'stat cards should flow as a wrapping row');
  assert(layout.cardWidth > 120, 'stat cards should be wide enough to read');
  assert(layout.radius !== '0px');
  assert(layout.heading === '用量统计');
  assert(layout.nav === true, 'nav item should be active on the usage page');
  assert.deepEqual(errors, [], 'page threw runtime exceptions');

  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  mkdirSync('output', { recursive: true });
  writeFileSync('output/usage-page.png', Buffer.from(shot.data, 'base64'));
  console.log('usage-page PASS');
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();
}
