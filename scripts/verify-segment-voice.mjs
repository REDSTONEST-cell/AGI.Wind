// Headless Chromium check for per-segment voice + human-like typing pace.
// The real gateway is only used for boot/auth/static files; chat, SSE and speech
// are mocked in the page so the live conversation is never touched.
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const BASE = process.env.BASE || 'http://127.0.0.1:18440';
const port = 9361;
const AUDIO_ASSET = 'b7b1d1c5-fc54-4287-843f-434c6b37af17';
const IMAGE_ASSET = 'ac9a9e33-4834-4273-8507-1f6c0f9dfb08';
const chromePath = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const browser = spawn(chromePath, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--autoplay-policy=no-user-gesture-required',
  `--user-data-dir=${process.env.TEMP}/agiwind-segment-${Date.now()}`,
  `--remote-debugging-port=${port}`, 'about:blank',
], { stdio: 'ignore', windowsHide: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let ws, sequence = 0;
const pending = new Map(), errors = [];

function send(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(Error(method + ' timeout')); }, 20000);
    pending.set(id, {
      resolve: (value) => { clearTimeout(timer); resolve(value); },
      reject: (error) => { clearTimeout(timer); reject(error); },
    });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const value = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (value.exceptionDetails) throw Error(JSON.stringify(value.exceptionDetails));
  return value.result.value;
}

async function until(expression, tries = 150) {
  for (let i = 0; i < tries; i++) {
    try { if (await evaluate(expression)) return; } catch {}
    await sleep(100);
  }
  throw Error('condition failed: ' + expression);
}

const mock = `(() => {
  const originalFetch = window.fetch.bind(window);
  const mock = window.__agiMock = { started: Date.now(), streamDone: false, chatSeen: false, speechPolls: 0 };
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
  const event = (sequence, type, payload) => ({
    protocol_version: '1.0', event_id: 'mock-' + sequence, sequence,
    type, generation_id: 'gen-mock-1', epoch: 1, timestamp: new Date().toISOString(), payload,
  });
  const frames = [
    { delay: 40, data: event(1, 'memory.context', { source_ids: [], memory_count: 0, persona_version: 1 }) },
    { delay: 70, data: event(2, 'text.delta', { text: '第一句文字。' }) },
    { delay: 240, data: event(3, 'text.segment', { index: 0, text: '第一句文字', kind: 'text', complete: true }) },
    { delay: 80, data: event(4, 'text.delta', { text: '第二句是语音。' }) },
    { delay: 150, data: event(5, 'text.segment', { index: 1, text: '第二句是语音', kind: 'voice', complete: true, voice: { state: 'queued', segment_id: 'voice-mock-1', mode: 'always' } }) },
    { delay: 80, data: event(6, 'text.delta', { text: '第三句文字。' }) },
    { delay: 150, data: event(7, 'text.segment', { index: 2, text: '第三句文字', kind: 'text', complete: true }) },
    { delay: 60, data: event(8, 'sticker.sent', { attachment: { asset_id: '${IMAGE_ASSET}', kind: 'sticker', mime: 'image/png', name: '困困摸鱼', width: 1415, height: 901, trigger: 'library' } }) },
    { delay: 40, data: event(9, 'text.completed', {}) },
    { delay: 40, data: event(10, 'generation.completed', {}) },
  ];
  const sseBody = () => new ReadableStream({
    async start(controller) {
      const encoder = new TextEncoder();
      for (const frame of frames) {
        await new Promise((resolve) => setTimeout(resolve, frame.delay));
        controller.enqueue(encoder.encode('id: ' + frame.data.sequence + '\\nevent: ' + frame.data.type + '\\ndata: ' + JSON.stringify(frame.data) + '\\n\\n'));
      }
      mock.streamDone = true;
      controller.close();
    },
  });
  window.fetch = async (input, init = {}) => {
    const url = String(input && input.url ? input.url : input);
    if (url.includes('/api/v1/chat/requests')) {
      mock.chatSeen = true;
      return json({ request_id: 'request-mock-1', generation_id: 'gen-mock-1', conversation_id: 'conversation-mock-1', events_url: '/api/v1/generations/gen-mock-1/events', created_at: new Date().toISOString() }, 202);
    }
    if (url.includes('/api/v1/generations/gen-mock-1/events')) {
      return new Response(sseBody(), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }
    if (url.includes('/api/v1/generations/gen-mock-1/speech')) {
      mock.speechPolls += 1;
      if (Date.now() - mock.started < 900) {
        return json({ state: 'queued', segments: [{ segment_id: 'voice-mock-1', segment_index: 1, state: 'synthesizing', voice_id: 'yuwan', mode: 'always' }] });
      }
      return json({ state: 'ready', segments: [{ segment_id: 'voice-mock-1', segment_index: 1, state: 'ready', voice_id: 'yuwan', mode: 'always', emotion: '开心', attachment: { asset_id: '${AUDIO_ASSET}', kind: 'audio', mime: 'audio/wav', name: '语音', duration_ms: 5200, sample_rate: 24000, channels: 1, segment_index: 1, segment_id: 'voice-mock-1' } }] });
    }
    return originalFetch(input, init);
  };
})();`;

try {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`http://127.0.0.1:${port}/json/version`)).ok) break; } catch {}
    await sleep(100);
  }
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.onmessage = (event) => {
    const value = JSON.parse(event.data);
    if (value.id && pending.has(value.id)) {
      const entry = pending.get(value.id); pending.delete(value.id);
      value.error ? entry.reject(Error(JSON.stringify(value.error))) : entry.resolve(value.result);
    } else if (value.method === 'Runtime.exceptionThrown') {
      errors.push(value.params.exceptionDetails);
    }
  };
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: mock });
  await send('Page.navigate', { url: BASE + '/#/chat' });
  await until(`!!(document.getElementById('send') && document.getElementById('input') && document.getElementById('messages'))`);
  await sleep(1200);
  await evaluate(`(() => {
    window.__agiBase = document.querySelectorAll('#messages .msg[data-role="assistant"]').length;
    window.__agiTimeline = { text0: 0, voice: 0, text2: 0, image: 0 };
    const input = document.getElementById('input');
    input.value = '测试分段语音';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('composer').requestSubmit();
    return true;
  })()`);
  console.log('debug after submit', JSON.stringify(await evaluate(`({href: location.href, base: window.__agiBase, timeline: typeof window.__agiTimeline, appHidden: document.getElementById('app')?.hidden, chatSeen: window.__agiMock?.chatSeen, busy: document.getElementById('send')?.disabled})`)));

  const startedAt = Date.now();
  let finalSnapshot = null;
  for (let i = 0; i < 250; i++) {
    const snapshot = await evaluate(`(() => {
      const all = [...document.querySelectorAll('#messages .msg[data-role="assistant"]')];
      const items = all.slice(window.__agiBase || 0);
      const kinds = items.map((node) => node.dataset.messageKind || '');
      const texts = items.filter((node) => node.dataset.messageKind === 'text');
      const voice = items.find((node) => node.dataset.messageKind === 'audio');
      const image = items.find((node) => node.dataset.messageKind === 'image');
      const visible = (node) => !!node && !node.hidden;
      const timeline = window.__agiTimeline || (window.__agiTimeline = { text0: 0, voice: 0, text2: 0, image: 0 });
      if (!timeline.text0 && texts[0] && (texts[0].innerText || '').includes('第一句文字')) timeline.text0 = Date.now();
      if (!timeline.voice && visible(voice)) timeline.voice = Date.now();
      if (!timeline.text2 && texts[1] && (texts[1].innerText || '').includes('第三句文字') && visible(texts[1])) timeline.text2 = Date.now();
      if (!timeline.image && visible(image)) timeline.image = Date.now();
      return {
        kinds,
        count: items.length,
        firstText: texts[0] ? texts[0].innerText.trim() : '',
        thirdText: texts[1] ? texts[1].innerText.trim() : '',
        voiceText: items[1] ? items[1].innerText.trim() : '',
        voiceVisible: visible(voice),
        voiceFallback: voice ? voice.classList.contains('voice-fallback') : false,
        imageVisible: visible(image),
        done: !!window.__agiMock.streamDone,
        timeline,
        body: document.getElementById('messages').innerText,
        newText: items.map((node) => node.innerText || '').join('\\n'),
      };
    })()`);
    finalSnapshot = snapshot;
    if (snapshot.done && snapshot.kinds.length >= 4 && snapshot.imageVisible && snapshot.timeline.text2) break;
    await sleep(80);
  }
  assert(finalSnapshot, 'no snapshot');
  console.log('final snapshot', JSON.stringify({
    kinds: finalSnapshot.kinds,
    firstText: finalSnapshot.firstText,
    thirdText: finalSnapshot.thirdText,
    voiceVisible: finalSnapshot.voiceVisible,
    voiceFallback: finalSnapshot.voiceFallback,
    imageVisible: finalSnapshot.imageVisible,
    done: finalSnapshot.done,
    timeline: finalSnapshot.timeline,
    speechPolls: await evaluate('window.__agiMock.speechPolls'),
    errors: errors.length,
  }));
  console.log('items debug', JSON.stringify(await evaluate(`([...document.querySelectorAll('#messages .msg[data-role="assistant"]')].slice(window.__agiBase||0).map((node) => ({kind: node.dataset.messageKind, hidden: node.hidden, text: (node.querySelector('.text')?.textContent||'').slice(0,80), bubble: node.querySelector('.bubble')?.className})))`)));
  assert.deepEqual(finalSnapshot.kinds.slice(0, 4), ['text', 'audio', 'text', 'image']);
  assert.match(finalSnapshot.firstText, /第一句文字/);
  assert.match(finalSnapshot.thirdText, /第三句文字/);
  assert.equal(finalSnapshot.voiceFallback, false);
  assert.equal(finalSnapshot.voiceVisible, true);
  assert.equal(finalSnapshot.imageVisible, true);
  const { text0, voice, text2 } = finalSnapshot.timeline;
  assert(text0 > 0 && voice > 0 && text2 > 0, 'typing timeline incomplete');
  assert(voice - text0 >= 150, 'voice bubble appeared too soon after the first text');
  assert(text2 - voice >= 100, 'third text did not wait for the voice bubble');
  assert.equal(errors.length, 0, 'browser JS exceptions: ' + JSON.stringify(errors).slice(0, 800));
  assert(!finalSnapshot.newText.includes('[表情包'));
  assert(!finalSnapshot.newText.includes('[情绪'));
  const elapsed = Date.now() - startedAt;
  assert(elapsed > 500, 'typing was too fast: ' + elapsed);
  await mkdir('.devtools', { recursive: true });
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  await writeFile('.devtools/segment-voice-client.png', Buffer.from(shot.data, 'base64'));
  console.log('Segment voice client: PASS (ordered text/voice/text, paced typing, sticker after, zero JS errors)');
} finally {
  try { ws?.close(); } catch {}
  browser.kill();
}
