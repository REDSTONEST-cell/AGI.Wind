// Controlled Chromium verification of the memory management page; only memory APIs are mocked.
import { spawn } from 'node:child_process';
import { writeFile, mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
const BASE = process.env.BASE || 'http://127.0.0.1:18440';
const port = 9358;
const browser = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--window-size=1440,1000', `--user-data-dir=${process.env.TEMP}/agiwind-memory-${Date.now()}`, `--remote-debugging-port=${port}`, 'about:blank'], { stdio: 'ignore', windowsHide: true });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let ws; let seq = 0; const pending = new Map(); const errors = [];
async function send(method, params = {}) { const id = ++seq; return new Promise((resolve, reject) => { const timer = setTimeout(() => { pending.delete(id); reject(Error(method + ' timeout')); }, 15000); pending.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); }, reject }); ws.send(JSON.stringify({ id, method, params })); }); }
async function evaluate(expression) { const value = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (value.exceptionDetails) throw Error(JSON.stringify(value.exceptionDetails)); return value.result.value; }
async function until(expression) { for (let i = 0; i < 100; i++) { if (await evaluate(expression)) return; await sleep(100); } throw Error('condition failed: ' + expression); }
const mock = `(() => {
 const original = window.fetch.bind(window);
 const m1 = '00000000-0000-4000-8000-0000000000a1';
 const m2 = '00000000-0000-4000-8000-0000000000a2';
 const ev = '00000000-0000-4000-8000-0000000000b1';
 let items = [
  {id:m1,kind:'preference',content:'用户希望被称为小风',epistemic_type:'user_asserted',status:'active',confidence:0.9,created_at:'2026-10-25T08:00:00+00:00',source_count:1,importance:0.82,access_count:2,expires_at:'2026-12-01T08:00:00+00:00',decay_type:'exponential',last_accessed_at:'2026-10-25T09:00:00+00:00'},
  {id:m2,kind:'episode',content:'一起去海边聊天',epistemic_type:'observed',status:'active',confidence:0.7,created_at:'2026-10-25T07:00:00+00:00',source_count:1,importance:0.35,access_count:0,expires_at:'2026-11-01T07:00:00+00:00',decay_type:'exponential',last_accessed_at:null},
 ];
 const detail = {id:m1,kind:'preference',content:'用户希望被称为小风',epistemic_type:'user_asserted',status:'active',confidence:0.9,created_at:'2026-10-25T08:00:00+00:00',importance:0.82,access_count:2,expires_at:'2026-12-01T08:00:00+00:00',decay_type:'exponential',last_accessed_at:'2026-10-25T09:00:00+00:00',source_refs:[{event_id:ev,json_pointer:'/text',start_offset:2,end_offset:8,quote_hash:'x',available:true,quote:'以后叫我小风',occurred_at:'2026-10-25T08:00:00+00:00'}]};
 const jobs = [{id:'00000000-0000-4000-8000-0000000000c1',state:'completed',attempts:1,created_at:'2026-10-25T08:00:01+00:00',available_at:'2026-10-25T08:00:01+00:00',last_error_code:null,result:{events:2,stored:1,ignored:0,quarantined:0},usage:{calls:1}}];
 const ok = (value, status = 200) => new Response(JSON.stringify(value), {status, headers:{'Content-Type':'application/json'}});
 window.__memory = {items, patchCalls:[], importanceCalls:0, queued:0, maintained:0, settingsSaved:0};
 let memorySettings = {extraction_enabled:true,batch_events:8,max_candidates:8,timeout_seconds:30,maintenance_enabled:true,maintenance_hours:6,extractor:'dialogue_fallback',embedding:'keyword_only'};
 window.fetch = async (input, init = {}) => {
  const url = String(input); const method = init.method || 'GET';
  if (url.includes('/api/v1/memory/stats?')) return ok({counts:{active:2,hidden:0,expired:0,superseded:0},total:2,kinds:{preference:1,episode:1},importance_buckets:[{range:'0-10%',count:0},{range:'10-20%',count:0},{range:'20-30%',count:0},{range:'30-40%',count:1},{range:'40-50%',count:0},{range:'50-60%',count:0},{range:'60-70%',count:0},{range:'70-80%',count:0},{range:'80-90%',count:1},{range:'90-100%',count:0}],jobs:{completed:1},jobs_total:1,visible_events:10,pending_events:8,settings:memorySettings});
  if (url.includes('/api/v1/memory/settings')) {
   if (method === 'PATCH') { memorySettings = {...memorySettings, ...JSON.parse(init.body)}; window.__memory.settingsSaved += 1; return ok(memorySettings); }
   return ok(memorySettings);
  }
  if (url.includes('/api/v1/memory/recall-test')) {
   return ok([{memory_id:m1,content:'用户希望被称为小风',score:0.91,routes:['structured'],kind:'preference',status:'active',importance:0.82,freshness:0.79}]);
  }
  if (url.includes('/api/v1/memory/maintenance')) { window.__memory.maintained += 1; return ok({expired:0,checked_at:'2026-10-25T08:00:02+00:00'}); }
  if (url.includes('/api/v1/memory/extractions')) {
   if (method === 'POST') { window.__memory.queued += 1; return ok({queued:true,job_id:'00000000-0000-4000-8000-0000000000c2'}, 201); }
   return ok(jobs);
  }
  if (url.includes('/api/v1/memory/items/')) {
   const id = decodeURIComponent(url.split('/api/v1/memory/items/')[1].split('?')[0]);
   if (method === 'PATCH') {
    const body = JSON.parse(init.body); window.__memory.patchCalls.push({id, body});
    if (body.importance !== undefined) { window.__memory.importanceCalls += 1; detail.importance = body.importance; return ok({memory_id:id,status:detail.status,importance:body.importance,memory_revision:2}); }
    const target = items.find((item) => item.id === id); if (target) target.status = body.status; return ok({memory_id:id,status:body.status,memory_revision:2});
   }
   return ok(detail);
  }
  if (url.includes('/api/v1/memory/items?')) {
   const params = new URLSearchParams(url.split('?')[1]);
   const status = params.get('status'); const query = (params.get('q') || '').trim(); const sort = params.get('sort') || 'created';
   let rows = items.filter((item) => !status || item.status === status);
   if (query) rows = rows.filter((item) => item.content.includes(query));
   if (sort === 'importance') rows = [...rows].sort((a, b) => b.importance - a.importance);
   return ok(rows);
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
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) { const promise = pending.get(message.id); pending.delete(message.id); if (message.error) promise.reject(Error(JSON.stringify(message.error))); else promise.resolve(message.result); }
    else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
  };
  await send('Page.enable'); await send('Runtime.enable'); await send('Page.addScriptToEvaluateOnNewDocument', { source: mock });
  await send('Page.navigate', { url: BASE + '/' }); await until(`!document.getElementById('app').hidden`);
  await evaluate(`location.hash='#/memory'`);
  await until(`document.querySelectorAll('#memories .memory-item').length===2`);
  assert.equal(await evaluate(`document.querySelectorAll('#memory-kind option').length`), 9);
  assert.equal(await evaluate(`document.querySelectorAll('#memory-status option').length`), 6);
  assert.equal(await evaluate(`document.querySelectorAll('#memory-jobs .memory-job').length`), 1);
  assert.match(await evaluate(`document.getElementById('memory-jobs').textContent`), /写入 1/);
  assert.equal(await evaluate(`document.getElementById('memory-stat-active').textContent`), '2');
  assert.equal(await evaluate(`document.querySelectorAll('#memory-importance-chart .memory-bar').length`), 10);
  assert.equal(await evaluate(`document.querySelectorAll('#memory-kind-chart .memory-bar').length`), 2);
  assert.match(await evaluate(`document.getElementById('memory-embedding-chip').textContent`), /向量：未接入/);
  assert(await evaluate(`document.getElementById('memory-extraction-enabled').checked`));
  await evaluate(`document.getElementById('memory-extraction-enabled').checked=false;document.getElementById('memory-settings-form').requestSubmit()`);
  await until(`window.__memory.settingsSaved===1`);
  assert.equal(await evaluate(`window.__memory.settingsSaved`), 1);
  assert(await evaluate(`!!document.getElementById('memory-extract-now')`));
  await evaluate(`document.getElementById('memory-extract-now').click()`);
  await until(`window.__memory.queued===1`);
  assert.match(await evaluate(`document.getElementById('memory-hint').textContent`), /筛选/);
  await evaluate(`document.getElementById('memory-recall-query').value='叫我什么';document.getElementById('memory-recall-form').requestSubmit()`);
  await until(`document.querySelectorAll('#memory-recall-results .recall-hit').length===1`);
  assert.match(await evaluate(`document.getElementById('memory-recall-results').textContent`), /结构化/);
  assert.match(await evaluate(`document.getElementById('memory-recall-results').textContent`), /分数/);
  await evaluate(`document.getElementById('memory-maintain').click()`);
  await until(`window.__memory.maintained===1`);
  await evaluate(`document.querySelector('#memories .memory-item').click()`);
  await until(`!document.getElementById('memory-detail').hidden`);
  assert.match(await evaluate(`document.getElementById('memory-detail').textContent`), /以后叫我小风/);
  assert.match(await evaluate(`document.getElementById('memory-detail').textContent`), /重要度/);
  assert(await evaluate(`[...document.querySelectorAll('#memory-detail button')].some((button)=>button.textContent==='纠正')`));
  await evaluate(`[...document.querySelectorAll('#memory-detail button')].find((button)=>button.textContent==='标为重要').click()`);
  await until(`window.__memory.importanceCalls===1`);
  await until(`document.getElementById('memory-detail').textContent.includes('90%')`);
  await mkdir('.devtools', { recursive: true });
  await send('Page.captureScreenshot', { format: 'png' }).then((shot) => writeFile('.devtools/memory-client-1440.png', Buffer.from(shot.data, 'base64')));
  await evaluate(`[...document.querySelectorAll('#memory-detail button')].find((button)=>button.textContent==='纠正').click()`);
  await until(`!document.getElementById('memory-edit').hidden`);
  assert.equal(await evaluate(`document.querySelectorAll('#correction-source option').length`), 1);
  assert.match(await evaluate(`document.getElementById('correction-quote').textContent`), /以后叫我小风/);
  await evaluate(`document.getElementById('cancel-correction').click()`);
  assert(await evaluate(`document.getElementById('memory-edit').hidden`));
  await evaluate(`[...document.querySelectorAll('#memory-detail button')].find((button)=>button.textContent.includes('隐藏')).click()`);
  await until(`document.querySelectorAll('#memories .memory-item').length===1`);
  assert.equal(await evaluate(`window.__memory.patchCalls.some((call)=>call.body.status==='hidden')`), true);
  assert(await evaluate(`document.getElementById('memory-detail').hidden`));
  await evaluate(`document.getElementById('memory-status').value='hidden';document.getElementById('memory-search').requestSubmit()`);
  await until(`document.querySelectorAll('#memories .memory-item').length===1`);
  assert.match(await evaluate(`document.getElementById('memories').textContent`), /用户希望被称为小风/);
  await send('Page.captureScreenshot', { format: 'png' }).then((shot) => writeFile('.devtools/memory-client-hidden.png', Buffer.from(shot.data, 'base64')));
  assert.deepEqual(errors, []);
  console.log('memory client verification: PASS');
} finally {
  try { ws?.close(); } catch {}
  browser.kill();
}
