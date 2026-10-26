// Controlled Chromium verification: all writes intercepted; existing conversations untouched.
import { spawn } from 'node:child_process';
import { writeFile, mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
const BASE=process.env.BASE || 'http://127.0.0.1:18440';
const port=9357;
const browser=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',`--user-data-dir=${process.env.TEMP}/agiwind-v7-${Date.now()}`,`--remote-debugging-port=${port}`,'about:blank'],{stdio:'ignore',windowsHide:true});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let ws;let seq=0;const pending=new Map();const errors=[];
async function send(method,params={}) {const id=++seq;return new Promise((resolve,reject)=>{const timeout=setTimeout(()=>{pending.delete(id);reject(Error(method+' timeout'));},15000);pending.set(id,{resolve:v=>{clearTimeout(timeout);resolve(v);},reject});ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const value=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(value.exceptionDetails)throw Error(JSON.stringify(value.exceptionDetails));return value.result.value;}
async function until(expression){for(let i=0;i<80;i++){if(await evaluate(expression))return;await sleep(100);}throw Error('condition failed: '+expression);}
const mock=`(() => {
 const original=window.fetch.bind(window);let rows=[];let history=JSON.parse(sessionStorage.getItem('__v7history')||'[]');
 const aid='00000000-0000-4000-8000-000000000081';const sid='00000000-0000-4000-8000-000000000082';
 const image={asset_id:aid,kind:'sticker',name:'小猫',mime:'image/gif',width:1,height:1};
 const ok=(v,status=200)=>new Response(JSON.stringify(v),{status,headers:{'Content-Type':'application/json'}});
 window.__v7={requests:[],history:[]};
 window.__v7.setHistory=value=>{history=value;window.__v7.history=value;};
 let persona={companion_id:'00000000-0000-4000-8000-000000000090',version:1,core:{name:'测试角色',user_name:'测试用户',description:'合成界面验证',avatar_asset_id:aid},style:{},relationship:{},commitments:[]};
 window.fetch=async(input,init={})=>{
  const url=String(input);const method=init.method||'GET';
  if(url.includes('/api/v1/companions/')&&url.endsWith('/persona')){
   if(method==='PATCH'){window.__v7.personaPatch=JSON.parse(init.body);persona={...persona,...window.__v7.personaPatch.patch,version:persona.version+1};}
   return ok(persona);
  }
  if(url.includes('/api/v1/assets/')&&method==='GET')return new Response(Uint8Array.from(atob('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'),c=>c.charCodeAt(0)),{headers:{'Content-Type':'image/gif'}});
  if(url.endsWith('/api/v1/assets')&&method==='POST'){
   const file=init.body.get('file');const bitmap=await createImageBitmap(file);const canvas=document.createElement('canvas');canvas.width=bitmap.width;canvas.height=bitmap.height;const ctx=canvas.getContext('2d');ctx.drawImage(bitmap,0,0);window.__v7.lastUpload={width:bitmap.width,height:bitmap.height,type:file.type,pixel:Array.from(ctx.getImageData(Math.floor(bitmap.width/2),Math.floor(bitmap.height/2),1,1).data)};bitmap.close();
   return ok({id:aid,mime:file.type,metadata:{width:canvas.width,height:canvas.height}},201);
  }
  if(url.includes('/api/v1/stickers')){
   if(method==='GET')return ok(rows);
   if(method==='POST'){rows=[{id:sid,name:'小猫',tags:[],state:'active',source:'uploaded',use_count:0,asset_id:aid,attachment:image}];return ok(rows[0],201);}
   if(method==='PATCH'){Object.assign(rows[0],JSON.parse(init.body));return ok(rows[0]);}
   if(method==='DELETE'){rows=[];return ok({deleted:true});}
  }
  if(url.includes('/api/v1/chat/requests')&&method==='POST'){
   const request=JSON.parse(init.body);window.__v7.requests.push(request);
   return ok({generation_id:sid,epoch:1,events_url:'/v7-mock/events'},202);
  }
  if(url.includes('/v7-mock/events')){
   if(window.__v7.failStream){
    if(init.headers?.['Last-Event-ID'])throw Error('模拟重连失败');
    let reads=0;
    const frame=window.__v7.failStream==='image'?'id: 1\\nevent: sticker.sent\\ndata: '+JSON.stringify({sequence:1,payload:{attachment:image,trigger:'library'}})+'\\n\\n':'id: 1\\nevent: text.delta\\ndata: {"sequence":1,"payload":{"text":"保留半句"}}\\n\\n';
    return new Response(new ReadableStream({pull(c){if(reads++===0)c.enqueue(new TextEncoder().encode(frame));else c.error(Error('模拟断线'));}}),{headers:{'Content-Type':'text/event-stream'}});
   }
   const trace=[];const frames=[];const emit=(type,payload)=>frames.push('id: '+(frames.length+1)+'\\nevent: '+type+'\\ndata: '+JSON.stringify({type,sequence:frames.length+1,payload})+'\\n\\n');
   const tool=(id,tool,stage)=>{emit('tool.started',{step_id:id,tool,stage,summary:tool});emit('tool.completed',{step_id:id,tool,stage,status:'completed',summary:'已完成 '+tool});trace.push({id,type:'tool',tool,stage,status:'completed',summary:'已完成 '+tool});};
   tool('observe','observe_image','observe');emit('reasoning.delta',{stage:'reply',text:'观察到一只猫'});trace.push({type:'reasoning',stage:'reply',text:'观察到一只猫'});emit('text.delta',{text:'这只猫真可爱。'});emit('text.delta',{text:'？你觉得呢？'});tool('reply','reply','reply');emit('text.completed',{});emit('reasoning.delta',{stage:'reflect',text:'适合收藏'});trace.push({type:'reasoning',stage:'reflect',text:'适合收藏'});tool('save','save_sticker','reflect');tool('send','send_sticker','reflect');emit('sticker.sent',{attachment:image,trigger:'mimic'});emit('generation.completed',{});
   history=[{role:'system',kind:'vision_observation',content:{text:'不应显示的内部观察'}},{id:'user-fixture',generation_id:sid,role:'user',kind:'user_input',content:{text:''},attachments:[image]},{id:'assistant-fixture',generation_id:sid,role:'assistant',kind:'assistant_output',content:{text:'这只猫真可爱。？你觉得呢？',trace,attachments:[image]},attachments:[image],trace}];window.__v7.history=history;sessionStorage.setItem('__v7history',JSON.stringify(history));
   return new Response(new ReadableStream({async start(c){for(const frame of frames){c.enqueue(new TextEncoder().encode(frame));await new Promise(r=>setTimeout(r,15));}c.close();}}),{headers:{'Content-Type':'text/event-stream'}});
  }
  if(url.includes('/events?'))return ok(history);
  if(method!=='GET' && !url.includes('/client/bootstrap') && !url.includes('/client/session'))throw Error('Unexpected write: '+url);
  return original(input,init);
 };
})();`;
try {
 for(let i=0;i<100;i++){try{if((await fetch(`http://127.0.0.1:${port}/json/version`)).ok)break;}catch{}await sleep(100);}
 const target=await(await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j;});
 ws.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const promise=pending.get(message.id);pending.delete(message.id);if(message.error)promise.reject(Error(JSON.stringify(message.error)));else promise.resolve(message.result);}else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails);else if(message.method==='Fetch.requestPaused'){send('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:'image/gif'}],body:'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'}).catch(error=>errors.push(error.message));}};
 await send('Page.enable');await send('Runtime.enable');await send('Fetch.enable',{patterns:[{urlPattern:'*/api/v1/assets/*'}]});await send('Page.addScriptToEvaluateOnNewDocument',{source:mock});
 await send('Page.navigate',{url:BASE+'/'});await until(`!document.getElementById('app').hidden`);
 await until(`document.getElementById('chat-images') !== null`);
 await evaluate(`location.hash='#/soul'`);await until(`document.getElementById('persona-name').value==='测试角色'`);
 await evaluate(`for(let i=0;i<10;i++)document.getElementById('persona-name').dispatchEvent(new Event('input',{bubbles:true}))`);await sleep(200);
 assert.equal(await evaluate(`document.querySelectorAll('#persona-avatar img').length`),1);
 assert(await evaluate(`(()=>{const img=document.querySelector('#persona-avatar img').getBoundingClientRect();const button=document.getElementById('persona-avatar').getBoundingClientRect();return img.height<=button.height&&button.height>=72;})()`));
 await until(`document.getElementById('user-avatar-file')!==null`);
 await evaluate(`location.hash='#/chat'`);
 assert.equal(await evaluate(`document.querySelector('.sidebar .brand')`),null);
 assert.equal(await evaluate(`document.querySelector('label[for="chat-images"]')?.textContent.trim()`),'+');
 await mkdir('.devtools',{recursive:true});
 await writeFile('.devtools/v7-fixture.gif',Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7','base64'));
 await send('DOM.enable');const doc=await send('DOM.getDocument');const input=await send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#chat-images'});
 await send('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[process.cwd().replaceAll('\\','/')+'/.devtools/v7-fixture.gif']});
 await until(`document.querySelectorAll('#pending-images img').length===1 && !document.getElementById('send').disabled`);
 await evaluate(`document.getElementById('composer').requestSubmit()`);
 await until(`document.querySelector('#messages .msg[data-role="assistant"] .text')?.textContent==='这只猫真可爱' && !document.getElementById('send').disabled`);
 assert(await evaluate(`document.querySelectorAll('#messages .msg.animate-in').length>=2`));
 assert(await evaluate(`parseFloat(getComputedStyle(document.querySelector('#messages .msg.animate-in')).animationDuration)>=0.2`));
 assert.equal(await evaluate(`window.__v7.requests[0].input.asset_ids.length`),1);
 assert.equal(await evaluate(`window.__v7.requests[0].input.text`),'');
 await evaluate(`(() => {const transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'),c=>c.charCodeAt(0))],'clipboard.gif',{type:'image/gif'}));document.getElementById('input').dispatchEvent(new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true}));})()`);
 await until(`document.querySelectorAll('#pending-images img').length===1 && !document.getElementById('send').disabled`);
 await evaluate(`document.querySelector('#pending-images button.btn').click()`);
 assert.equal(await evaluate(`document.querySelectorAll('#pending-images img').length`),0);
 const order=await evaluate(`Array.from(document.querySelector('#messages .msg[data-role="assistant"] .bubble').children).map(n=>n.className)`);
 assert.equal(await evaluate(`document.querySelectorAll('#messages .trace').length`),0);
 assert.deepEqual(await evaluate(`Array.from(document.querySelectorAll('#messages .msg[data-role="assistant"][data-message-kind="text"] .text')).map(n=>n.textContent)`),['这只猫真可爱','？你觉得呢']);
 assert.equal(await evaluate(`document.querySelectorAll('#messages .msg[data-role="assistant"][data-message-kind="image"]').length`),1);
 assert.equal(await evaluate(`document.querySelectorAll('#messages .msg[data-message-kind="text"] .message-images').length`),0);
 assert.equal(await evaluate(`document.querySelectorAll('#messages .message-images img').length`),2);
 await until(`Array.from(document.querySelectorAll('#messages .message-images img')).every(img=>img.complete&&img.naturalWidth>0)`);
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
 assert(await evaluate(`document.querySelector('#messages .avatar').getBoundingClientRect().width>=48`));
 assert(await evaluate(`document.querySelector('.chat-card').getBoundingClientRect().height>=550`));
 await evaluate(`document.getElementById('log-toggle').click()`);
 await until(`document.getElementById('term-output').textContent.includes('观察到一只猫')&&document.getElementById('term-output').textContent.includes('已完成 save_sticker')`);
 assert.equal(await evaluate(`(localStorage.getItem('agiwind-log-v1')||'').includes('观察到一只猫')`),false);
 await sleep(250);
 const handle=await evaluate(`(()=>{const r=document.getElementById('log-resize').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+100,width:document.getElementById('log-drawer').getBoundingClientRect().width};})()`);
 await send('Input.dispatchMouseEvent',{type:'mousePressed',x:handle.x,y:handle.y,button:'left',clickCount:1});
 await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:handle.x-100,y:handle.y,button:'left',buttons:1});
 await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:handle.x-100,y:handle.y,button:'left',clickCount:1});
 assert(await evaluate(`document.getElementById('log-drawer').getBoundingClientRect().width>=${handle.width+80}`));
 assert(await evaluate(`Number(localStorage.getItem('agiwind-log-width'))>=${handle.width+80}`));
 const logshot=await send('Page.captureScreenshot',{format:'png'});await writeFile('.devtools/v7-log-1440.png',Buffer.from(logshot.data,'base64'));
 await evaluate(`document.getElementById('log-close').click()`);
 await evaluate(`document.getElementById('refresh-page').click()`);
 await sleep(700);
 assert.equal(await evaluate(`document.querySelectorAll('#messages .msg').length`),4);
 assert.equal(await evaluate(`document.querySelectorAll('#messages .trace').length`),0);
 assert.equal(await evaluate(`document.getElementById('messages').textContent.includes('观察到一只猫')`),false);
 assert.equal(await evaluate(`document.getElementById('messages').textContent.includes('不应显示的内部观察')`),false);
 await evaluate(`window.__v7.setHistory(Array.from({length:40},(_,i)=>({id:'long-'+i,role:i%2?'assistant':'user',kind:i%2?'assistant_output':'user_input',content:{text:'这是一条用于检查长历史布局的消息'}})));document.getElementById('refresh-page').click()`);await sleep(300);
 for(const width of [1440,1024,390]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});await sleep(200);
  assert.equal(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),true);
  for(const id of ['input','chat-images','send'])assert.equal(await evaluate(`(()=>{const n=document.getElementById('${id}'),r=n.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return r.top>=0&&r.bottom<=innerHeight&&n.contains(document.elementFromPoint(x,y));})()`),true,'composer control must stay visible and clickable: '+id+' at '+width);
  await evaluate(`document.getElementById('log-toggle').click()`);await sleep(250);
  for(const id of ['input','chat-images','send'])assert.equal(await evaluate(`(()=>{const n=document.getElementById('${id}'),r=n.getBoundingClientRect();return n.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));})()`),true,'log must not cover composer: '+id+' at '+width);
  await evaluate(`document.getElementById('log-close').click()`);
  const shot=await send('Page.captureScreenshot',{format:'png'});await writeFile(`.devtools/v7-chat-${width}.png`,Buffer.from(shot.data,'base64'));
 }
 await evaluate(`window.__v7.setHistory(JSON.parse(sessionStorage.getItem('__v7history')));location.hash='#/stickers'`);await until(`document.querySelector('[data-page="stickers"].page')?.hidden===false`);
 const stickerInput=await send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#sticker-upload'});
 await send('DOM.setFileInputFiles',{nodeId:stickerInput.nodeId,files:[process.cwd().replaceAll('\\','/')+'/.devtools/v7-fixture.gif']});
 await until(`document.querySelectorAll('.sticker-card').length===1`);
 await evaluate(`location.hash='#/chat'`);await until(`document.querySelector('[data-page="chat"].page').hidden===false`);
 await evaluate(`document.querySelector('.nav-item[data-page="stickers"]').click()`);await until(`document.querySelector('[data-page="stickers"].page').hidden===false`);
 assert.equal(await evaluate(`document.querySelectorAll('.sticker-card').length`),1);
 await evaluate(`document.querySelector('[data-sticker-action="rename"]').click()`);await until(`!document.getElementById('modal').hidden`);
 await evaluate(`document.getElementById('modal-input').value='新名字';document.getElementById('modal-ok').click()`);await until(`document.getElementById('sticker-grid').textContent.includes('新名字')`);
 await evaluate(`document.querySelector('[data-sticker-action="tags"]').click()`);await until(`!document.getElementById('modal').hidden`);
 await evaluate(`document.getElementById('modal-input').value='开心,猫';document.getElementById('modal-ok').click()`);await until(`document.getElementById('sticker-grid').textContent.includes('开心')`);
 await evaluate(`document.querySelector('[data-sticker-action="hide"]').click()`);await until(`document.getElementById('sticker-grid').textContent.includes('恢复')`);
 await evaluate(`document.querySelector('[data-sticker-action="hide"]').click()`);await until(`document.getElementById('sticker-grid').textContent.includes('隐藏')`);
 await evaluate(`document.querySelector('.sticker-card img').click()`);await until(`document.querySelector('.image-lightbox')!==null`);await evaluate(`document.querySelector('.image-lightbox button').click()`);
 await evaluate(`document.querySelector('[data-sticker-action="delete"]').click()`);await until(`!document.getElementById('modal').hidden`);await evaluate(`document.getElementById('modal-ok').click()`);await until(`document.querySelectorAll('.sticker-card').length===0`);
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
 await evaluate(`location.hash='#/soul'`);await until(`document.querySelector('[data-page="soul"].page').hidden===false`);
 await evaluate(`(async()=>{const canvas=document.createElement('canvas');canvas.width=200;canvas.height=100;const ctx=canvas.getContext('2d');ctx.fillStyle='red';ctx.fillRect(0,0,100,100);ctx.fillStyle='blue';ctx.fillRect(100,0,100,100);const blob=await new Promise(r=>canvas.toBlob(r));const transfer=new DataTransfer();transfer.items.add(new File([blob],'wide-avatar.png',{type:'image/png'}));const input=document.getElementById('user-avatar-file');input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
 await until(`document.querySelector('.avatar-crop-dialog')!==null`);
 await evaluate(`(()=>{const zoom=document.getElementById('avatar-crop-zoom');zoom.value='2';zoom.dispatchEvent(new Event('input',{bubbles:true}));})()`);
 const crop=await evaluate(`(()=>{const r=document.querySelector('.avatar-crop-canvas').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
 await send('Input.dispatchMouseEvent',{type:'mousePressed',x:crop.x,y:crop.y,button:'left',buttons:1,clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:crop.x+100,y:crop.y,button:'left',buttons:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:crop.x+100,y:crop.y,button:'left',clickCount:1});
 await evaluate(`document.getElementById('avatar-crop-confirm').click()`);
 await until(`document.querySelector('.avatar-crop-dialog')===null&&document.querySelectorAll('#user-avatar img').length===1`);
 assert.deepEqual(await evaluate(`window.__v7.lastUpload`),{width:512,height:512,type:'image/png',pixel:[255,0,0,255]});
 await evaluate(`document.getElementById('save-persona').click()`);await until(`window.__v7.personaPatch?.patch?.core?.user_avatar_asset_id!==undefined`);
 assert.equal(await evaluate(`window.__v7.personaPatch.patch.core.user_avatar_asset_id`),'00000000-0000-4000-8000-000000000081');
 await evaluate(`location.hash='#/chat'`);await until(`document.getElementById('breadcrumb-page').textContent==='对话'`);await evaluate(`document.getElementById('refresh-page').click()`);await until(`document.querySelectorAll('#messages .msg').length===4`);
 assert.equal(await evaluate(`document.querySelectorAll('#messages .msg[data-role="user"] .avatar img').length`),1);
 await evaluate(`location.hash='#/soul'`);await until(`!document.getElementById('user-avatar-remove').hidden`);await evaluate(`document.getElementById('user-avatar-remove').click();document.getElementById('save-persona').click()`);
 await until(`window.__v7.personaPatch?.patch?.core?.user_avatar_asset_id===undefined`);
 await send('Page.reload',{ignoreCache:true});await until(`!document.getElementById('app').hidden`);await evaluate(`location.hash='#/chat'`);await until(`document.querySelectorAll('#messages .msg').length===4`);
 await evaluate(`document.getElementById('log-toggle').click()`);await until(`document.getElementById('term-output').textContent.includes('观察到一只猫')&&document.getElementById('term-output').textContent.includes('这只猫真可爱。')`);await evaluate(`document.getElementById('log-close').click()`);
 await evaluate(`location.hash='#/chat';window.__v7.failStream=true;document.getElementById('input').value='断线测试';document.getElementById('composer').requestSubmit()`);
 await until(`!document.getElementById('send').disabled`);
 assert(await evaluate(`Array.from(document.querySelectorAll('#messages .msg[data-role="assistant"] .text')).at(-1).textContent.includes('保留半句')`));
 await evaluate(`window.__v7.failStream='image';document.getElementById('input').value='只有图片后断线';document.getElementById('composer').requestSubmit()`);await until(`!document.getElementById('send').disabled`);
 assert.equal(await evaluate(`Array.from(document.querySelectorAll('#messages .msg[data-message-kind="image"] .text')).at(-1).hidden`),false);
 assert.deepEqual(errors,[]);
 console.log('PASS: image-only input, ordered traces, attachments, history, library CRUD/lightbox, widths 1440/1024/390; 0 JS errors');
} finally {ws?.close();browser.kill();}
