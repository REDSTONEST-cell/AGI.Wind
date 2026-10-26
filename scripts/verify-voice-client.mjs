// Read-only Chromium check for the voice switch, probability popover and local TTS panel.
// It does not send a chat message or change the live probability.
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const BASE = process.env.BASE || 'http://127.0.0.1:18440';
const port = 9358;
const chrome = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const browser = spawn(chrome, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--user-data-dir=${process.env.TEMP}/agiwind-voice-${Date.now()}`,
  `--remote-debugging-port=${port}`, 'about:blank',
], { stdio: 'ignore', windowsHide: true });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let ws;
let sequence = 0;
const pending = new Map();
const errors = [];

async function send(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { pending.delete(id); reject(Error(method + ' timeout')); }, 15000);
    pending.set(id, { resolve: (value) => { clearTimeout(timeout); resolve(value); }, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function until(expression) {
  for (let i = 0; i < 120; i++) {
    if (await evaluate(expression)) return;
    await sleep(100);
  }
  throw Error('condition failed: ' + expression);
}

try {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`http://127.0.0.1:${port}/json/version`)).ok) break; } catch {}
    await sleep(100);
  }
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const promise = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) promise.reject(Error(JSON.stringify(message.error)));
      else promise.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') {
      errors.push(message.params.exceptionDetails);
    }
  };
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: BASE + '/' });
  await until(`!document.getElementById('app').hidden`);
  await until(`document.getElementById('voice-toggle') !== null`);
  assert.equal(await evaluate(`document.getElementById('voice-toggle').getAttribute('aria-checked')`), 'false');

  await evaluate(`document.getElementById('voice-toggle').click()`);
  assert.equal(await evaluate(`document.getElementById('voice-toggle').getAttribute('aria-checked')`), 'true');
  assert.equal(await evaluate(`localStorage.getItem('agiwind-voice-always')`), '1');
  await evaluate(`document.getElementById('voice-toggle').click()`);
  assert.equal(await evaluate(`document.getElementById('voice-toggle').getAttribute('aria-checked')`), 'false');

  await until(`document.querySelectorAll('#messages .msg').length > 0`);
  await sleep(600);
  const historyAudio = await evaluate(`document.querySelectorAll('#messages .msg[data-message-kind="audio"]').length`);
  if (historyAudio > 0) {
    await until(`document.querySelector('#messages .audio-duration')?.textContent !== '--:--'`);
    assert.equal(await evaluate(`document.querySelector('#messages .audio-message')?.classList.contains('audio-failed')`), false);
    await evaluate(`document.querySelector('#messages .msg[data-message-kind="audio"]').scrollIntoView({block:'center'})`);
    await sleep(300);
    await mkdir('.devtools', { recursive: true });
    const chatShot = await send('Page.captureScreenshot', { format: 'png' });
    await writeFile('.devtools/voice-chat-live.png', Buffer.from(chatShot.data, 'base64'));
  }

  await evaluate(`location.hash='#/model'`);
  await until(`!document.querySelector('.page[data-page="model"]').hidden`);
  await until(`document.querySelectorAll('.model-tab').length > 0`);
  await evaluate(`(() => { const button=Array.from(document.querySelectorAll('.model-tab')).find((item)=>item.textContent.includes('文字转语音')); if(!button) throw new Error('TTS tab missing'); button.click(); })()`);
  await until(`!document.getElementById('tts-settings').hidden`);
  await until(`document.getElementById('tts-availability').textContent.length > 0`);
  const availability = await evaluate(`document.getElementById('tts-availability').textContent`);
  assert.match(availability, /可用/);
  const options = await evaluate(`Array.from(document.querySelectorAll('#tts-voice option')).map((option)=>option.value)`);
  assert(options.includes('yuwan'), 'yuwan voice must be selectable');
  await evaluate(`document.getElementById('tts-settings').scrollIntoView({block:'start'})`);
  await sleep(250);
  await mkdir('.devtools', { recursive: true });
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  await writeFile('.devtools/voice-controls-live.png', Buffer.from(shot.data, 'base64'));
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    ok: true,
    availability,
    voices: options,
    history_audio_bubbles: historyAudio,
    screenshot: '.devtools/voice-controls-live.png',
    js_errors: errors.length,
  }, null, 2));
} finally {
  try { ws?.close(); } catch {}
  browser.kill();
}
