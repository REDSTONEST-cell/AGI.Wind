// Run against an isolated test Gateway, never against the user's live conversation.
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const BASE = process.env.BASE || 'http://127.0.0.1:18441';
const port = 9359;
const browser = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--window-size=1440,1000', `--user-data-dir=${process.env.TEMP}/agiwind-agent-${Date.now()}`, `--remote-debugging-port=${port}`, 'about:blank'], { stdio: 'ignore', windowsHide: true });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let ws, seq = 0; const pending = new Map(), errors = [];
async function send(method, params = {}) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(Error(method + ' timeout')); }, 15000);
    pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value); }, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const value = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (value.exceptionDetails) throw Error(JSON.stringify(value.exceptionDetails));
  return value.result.value;
}
async function until(expression) {
  for (let i = 0; i < 100; i++) { if (await evaluate(expression)) return; await sleep(100); }
  throw Error('condition failed: ' + expression);
}
const mock = `(() => {
 const original=window.fetch.bind(window);
 window.__agent={saved:[],value:{enabled:false,memory_search_enabled:true,sticker_search_enabled:true,sticker_send_enabled:true,web_search_enabled:false,file_access_mode:'ask',command_access_mode:'disabled',command_timeout_seconds:30,command_max_output_chars:8000,max_rounds:3,max_tool_calls:6,tool_timeout_seconds:15,total_timeout_seconds:90,max_result_chars:6000,search_backend:'searxng',search_url:'',search_max_results:5,search_key_configured:false,search_ready:false,model_tools_available:true,tools:[]}};
 window.fetch=async(input,init={})=>{
  const url=String(input?.url||input);
  if(!url.includes('/api/v1/agent/settings'))return original(input,init);
  const state=window.__agent;
  if(init.method==='PATCH'){
   const body=JSON.parse(init.body);state.saved.push({...body,api_key:body.api_key?'[redacted]':undefined});
   if(body.api_key)state.value.search_key_configured=true;
   if(body.clear_key)state.value.search_key_configured=false;
   delete body.api_key;delete body.clear_key;Object.assign(state.value,body);
  }
  state.value.search_ready=state.value.web_search_enabled&&(state.value.search_backend==='brave'?state.value.search_key_configured:!!state.value.search_url);
  state.value.tools=[{label:'记忆查询',available:state.value.memory_search_enabled},{label:'联网检索',available:state.value.search_ready}];
  return new Response(JSON.stringify(state.value),{status:200,headers:{'Content-Type':'application/json'}});
 };
})();`;
try {
  for (let i=0;i<100;i++) { try { if((await fetch(`http://127.0.0.1:${port}/json/version`)).ok)break; } catch {} await sleep(100); }
  const target=await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'})).json();
  ws=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=event=>{const value=JSON.parse(event.data);if(value.id&&pending.has(value.id)){const entry=pending.get(value.id);pending.delete(value.id);value.error?entry.reject(Error(JSON.stringify(value.error))):entry.resolve(value.result);}else if(value.method==='Runtime.exceptionThrown')errors.push(value.params.exceptionDetails);};
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.addScriptToEvaluateOnNewDocument',{source:mock});
  await send('Page.navigate',{url:BASE+'/#/agent'});
  await until(`!document.getElementById('app').hidden && document.getElementById('agent-state').textContent.includes('已关闭')`);
  assert.equal(await evaluate(`document.querySelector('[data-page="agent"].nav-item').getAttribute('aria-current')`),'page');
  await evaluate(`(()=>{const f=document.getElementById('agent-settings-form');f.elements.enabled.checked=true;f.elements.web_search_enabled.checked=true;f.elements.search_backend.value='brave';f.elements.search_backend.dispatchEvent(new Event('change'));f.elements.api_key.value='synthetic-browser-secret';f.elements.max_rounds.value='2';f.requestSubmit();})()`);
  await until(`document.getElementById('agent-settings-hint').textContent==='已保存'`);
  assert.equal(await evaluate(`document.querySelector('[name="api_key"]').value`),'');
  assert.equal(await evaluate(`document.getElementById('agent-search-url-field').hidden`),true);
  assert.equal(await evaluate(`window.__agent.saved[0].max_rounds`),2);
  assert.equal(await evaluate(`window.__agent.saved[0].command_access_mode`),'disabled');
  assert.match(await evaluate(`document.getElementById('agent-search-state').textContent`),/已配置/);
  await evaluate(`document.getElementById('agent-refresh').click()`);
  await until(`document.querySelector('[name="max_rounds"]').value==='2'`);
  await mkdir('.devtools',{recursive:true});
  for(const width of [1440,390]){
    await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width===390});
    await sleep(150);
    assert(await evaluate(`document.documentElement.scrollWidth<=window.innerWidth+1`));
    await send('Page.captureScreenshot',{format:'png'}).then(shot=>writeFile('.devtools/agent-client-'+width+'.png',Buffer.from(shot.data,'base64')));
  }
  assert.deepEqual(errors,[]);
  console.log('Agent settings client: PASS (save, mask, refresh, desktop/mobile, zero JS errors)');
}finally{try{ws?.close();}catch{}browser.kill();}
