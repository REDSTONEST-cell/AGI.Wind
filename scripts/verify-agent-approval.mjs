// Headless check for the Agent approval dialog: pending item renders, allow posts the decision.
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const BASE = process.env.BASE || 'http://127.0.0.1:18440';
const port = 9377;
const chrome = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--user-data-dir=${process.env.TEMP}/agiwind-agent-approval-${Date.now()}`,
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

const mock = `(() => {
  const original = window.fetch.bind(window);
  const json = (value) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });
  window.__decisions = [];
  window.fetch = async (input, init = {}) => {
    const url = String(input && input.url ? input.url : input);
    if (url.includes('/api/v1/agent/approvals') && (!init.method || init.method === 'GET')) {
      return json({ items: [{ id: 'synthetic-approval', generation_id: 'synthetic-generation', operation: 'list_files', details: { path: 'C:/Users/wind/Desktop' }, created_at: new Date().toISOString() }] });
    }
    if (url.includes('/api/v1/agent/approvals/synthetic-approval') && init.method === 'POST') {
      window.__decisions.push(JSON.parse(init.body));
      return json({ ok: true });
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
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await send('Page.addScriptToEvaluateOnNewDocument', { source: mock });
  await send('Page.navigate', { url: BASE + '/#/chat' });
  await until(`!document.getElementById('app').hidden`);
  await until(`document.getElementById('agent-approval-dialog').open === true`);
  const details = await evaluate(`({ operation: document.getElementById('agent-approval-details').textContent, modal: document.querySelector('#agent-approval-dialog').hasAttribute('modal'), allow: document.getElementById('agent-approval-allow').textContent })`);
  console.log('dialog', JSON.stringify(details));
  assert(details.operation.includes('list_files') && details.operation.includes('Desktop'));
  assert.equal(details.modal, false, 'approval dialog must stay non-modal');
  await evaluate(`document.getElementById('agent-approval-allow').click()`);
  await until(`document.getElementById('agent-approval-dialog').open === false`);
  const decisions = await evaluate(`window.__decisions`);
  console.log('decisions', JSON.stringify(decisions));
  assert.equal(decisions.length, 1);
  assert.equal(decisions[0].allow, true);
  assert.deepEqual(errors, [], 'page threw runtime exceptions');
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  mkdirSync('output', { recursive: true });
  writeFileSync('output/agent-approval.png', Buffer.from(shot.data, 'base64'));
  console.log('agent approval client: PASS (render, non-modal, allow decision, zero JS errors)');
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();
}
