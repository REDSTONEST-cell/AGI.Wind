import test, {afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const core = await readFile(new URL('../app/src/agi_wind/api/client/call-audio.js',import.meta.url),'utf8');
let code = await readFile(new URL('../app/src/agi_wind/api/client/realtime-call.js',import.meta.url),'utf8');
code = code.replace("'./call-audio.js?v=20261026-call1'",JSON.stringify('data:text/javascript;base64,'+Buffer.from(core).toString('base64')));
code = code.replaceAll('import.meta.url', "'http://localhost:18440/client/realtime-call.js'");
const {createRealtimeCall} = await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
const tick = () => new Promise(resolve=>setImmediate(resolve));
const sleep = (ms) => new Promise(resolve=>setTimeout(resolve,ms));
const defer = () => { let resolve; const promise=new Promise(r=>{resolve=r;}); return {promise,resolve}; };
let cleanup;
afterEach(async()=>{ await cleanup?.(); cleanup=null; });

function environment({permission,accepting,closing,streaming,replyText='哥哥我听到你说话了，继续吧。'}={}) {
  const elements = new Map();
  globalThis.document={querySelector(id){
    if(!elements.has(id))elements.set(id,{textContent:'',hidden:true,disabled:false,setAttribute(){},addEventListener(type,handler){this[type]=handler;}});
    return elements.get(id);
  }};
  globalThis.window={addEventListener(){}};
  globalThis.location={href:'http://localhost:18440/'};
  globalThis.isSecureContext=true;
  const track={enabled:true,stopped:false,stop(){this.stopped=true;}};
  const media={getTracks:()=>[track],getAudioTracks:()=>[track]};
  const constraints=[];
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:options=>{constraints.push(options);return permission?permission.promise:Promise.resolve(media);}}}});
  const sources=[];
  const contexts=[];
  globalThis.AudioContext=class {
    constructor(){this.sampleRate=16000;this.currentTime=0;this.destination={};this.audioWorklet={addModule:async()=>{}};contexts.push(this);}
    async resume(){} async close(){this.closed=true;if(closing)await closing.promise;}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createBuffer(){return {copyToChannel(){}};}
    createBufferSource(){const s={connect(){},disconnect(){},start(){},stop(){this.stopped=true;}};sources.push(s);return s;}
  };
  const captures=[];
  globalThis.AudioWorkletNode=class {constructor(){this.port={};captures.push(this);}connect(){}disconnect(){}};
  const sockets=[];
  globalThis.WebSocket=class {
    static OPEN=1;
    constructor(){this.readyState=1;this.bufferedAmount=0;this.sent=[];sockets.push(this);queueMicrotask(()=>this.onopen?.());}
    send(data){this.sent.push(data);if(typeof data==='string'){
      const value=JSON.parse(data);
      if(value.ticket)queueMicrotask(()=>this.emit({type:'ready',speech_credits:16}));
      if(value.type==='cancel')queueMicrotask(()=>this.emit({type:'speech_reset',epoch:value.epoch,credits:16}));
    }}
    close(){this.readyState=3;this.onclose?.();}
    emit(data){this.onmessage?.({data:data instanceof ArrayBuffer?data:JSON.stringify(data)});}
  };
  const requests=[], turns=[], errors=[];
  const client={url:x=>x,async request(path,options){requests.push({path,options});return path.endsWith('status')?{available:true}:{ticket:'synthetic',call_id:'synthetic-call'};},
    chat(payload){requests.push({path:'chat',payload});return accepting?accepting.promise:Promise.resolve({generation_id:'synthetic',events_url:'/events'});},
    async stream(path,options){if(streaming)return streaming(path,options);for(const [type,payload]of [['text.delta',{text:replyText}],['text.completed',{}],['generation.completed',{}]])options.onEvent({type,data:{payload}});},
  };
  const call=createRealtimeCall(client,{prepare:async()=> 'conversation',conversationId:()=> 'conversation',
    beginTurn(text){const view={text,update(value){this.reply=value;},finish(state){this.state=state;}};turns.push(view);return view;},error:x=>errors.push(x)});
  cleanup=()=>call.stop();
  return {call,track,media,sockets,sources,contexts,requests,turns,errors,elements,captures,constraints};
}

test('hangup during microphone permission wait releases a late microphone grant',async()=>{
  const permission=defer(); const env=environment({permission});
  const starting=env.call.start();await tick();
  await env.call.stop();
  permission.resolve(env.media);await starting;
  assert.equal(env.track.stopped,true);
  assert.equal(env.call.active,false);
  assert.ok(env.contexts.every(x=>x.closed));
});

test('disconnect while waiting for microphone permission cannot leave capture active',async()=>{
  const permission=defer();const env=environment({permission});const starting=env.call.start();await tick();
  env.sockets[0].close();await tick();permission.resolve(env.media);await starting;
  assert.equal(env.call.active,false);assert.equal(env.track.stopped,true);
});

test('an old AudioContext close cannot hide a newly connected call',async()=>{
  const closing=defer();const env=environment({closing});await env.call.start();
  const stopping=env.call.stop();await tick();await env.call.start();
  closing.resolve();await stopping;
  assert.equal(env.call.active,true);assert.equal(env.elements.get('#call-panel').hidden,false);
  assert.equal(env.elements.get('#call-toggle').textContent,'挂断');await env.call.stop();
});

test('barge-in stops playback and ignores audio from the previous epoch',async()=>{
  const env=environment();await env.call.start();const ws=env.sockets[0];
  ws.emit({type:'speech_start'});ws.emit({type:'final',text:'哥哥来了',utterance:1});await tick();
  assert.equal(env.requests.find(x=>x.path==='chat').payload.output.voice,'off');
  assert.equal(env.turns[0].state,'completed');
  assert.ok(ws.sent.some(x=>typeof x==='string'&&JSON.parse(x).type==='speak'));
  const packet=new ArrayBuffer(100);new DataView(packet).setUint32(0,1,true);
  ws.emit(packet);assert.equal(env.sources.length,1);
  // VAD alone no longer cancels playback; only a stable recognized partial does.
  ws.emit({type:'speech_start'});assert.ok(!env.sources[0].stopped);
  ws.emit({type:'partial',text:'插话'});await sleep(320);ws.emit({type:'partial',text:'插话'});
  assert.equal(env.sources[0].stopped,true);
  ws.emit(packet);assert.equal(env.sources.length,1);
  await env.call.stop();assert.equal(env.track.stopped,true);assert.deepEqual(env.errors,[]);
});

test('a generation accepted after hangup is immediately cancelled',async()=>{
  const accepting=defer();const env=environment({accepting});await env.call.start();const ws=env.sockets[0];
  ws.emit({type:'speech_start'});ws.emit({type:'final',text:'测试一下',utterance:1});await tick();
  const stopping=env.call.stop();assert.equal(env.track.stopped,true);
  accepting.resolve({generation_id:'late-generation',events_url:'/events'});await stopping;
  assert.ok(env.requests.some(x=>x.path==='/api/v1/generations/late-generation/interrupt'));
  assert.equal(env.turns[0].state,'cancelled');assert.equal(env.call.active,false);
});

test('long replies respect speech credits and barge-in discards pending chunks',async()=>{
  const env=environment({replyText:'甲'.repeat(1600)});await env.call.start();const ws=env.sockets[0];
  ws.emit({type:'speech_start'});ws.emit({type:'final',text:'讲个故事',utterance:1});await tick();
  const count=()=>ws.sent.filter(x=>typeof x==='string'&&JSON.parse(x).type==='speak').length;
  assert.equal(count(),16);
  ws.emit({type:'speech_credit',epoch:1,credits:10});assert.equal(count(),26);
  ws.emit({type:'partial',text:'插话'});await sleep(320);ws.emit({type:'partial',text:'插话'});await tick();
  ws.emit({type:'speech_credit',epoch:1,credits:10});assert.equal(count(),26);
  assert.equal(env.call.active,true);await env.call.stop();
});

test('echo-cancelled microphone continues uploading while replying and playing',async()=>{
  const holding=defer();const env=environment({streaming:()=>holding.promise});await env.call.start();const ws=env.sockets[0];
  assert.equal(env.constraints[0].audio.echoCancellation,true);
  ws.emit({type:'speech_start'});ws.emit({type:'final',text:'开始回答',utterance:1});await tick();
  assert.equal(env.requests.find(item=>item.path==='chat').payload.device_id,'synthetic-call');
  const packet=new ArrayBuffer(12);new DataView(packet).setUint32(0,1,true);ws.emit(packet);
  const frame=new ArrayBuffer(2560);env.captures[0].port.onmessage({data:frame});
  assert.ok(ws.sent.includes(frame));
  await env.call.stop();holding.resolve();
});

test('speech start aborts the old SSE and final replaces it while late events stay silent',async()=>{
  const listeners=[];const holding=defer();
  const env=environment({streaming:(path,options)=>{listeners.push(options);return holding.promise;}});
  await env.call.start();const ws=env.sockets[0];
  ws.emit({type:'speech_start'});ws.emit({type:'final',text:'旧问题',utterance:1});await tick();
  ws.emit({type:'speech_start'});
  ws.emit({type:'partial',text:'插话'});await sleep(320);ws.emit({type:'partial',text:'插话'});await tick();
  assert.equal(listeners[0].signal.aborted,true);assert.equal(env.turns[0].state,'cancelled');
  ws.emit({type:'final',text:'插话的新问题',utterance:2});await tick();
  assert.equal(env.turns.length,2);assert.equal(env.turns[1].text,'插话的新问题');
  listeners[0].onEvent({type:'text.delta',data:{payload:{text:'迟到的旧回复。'}}});
  assert.equal(env.turns[0].reply,undefined);
  assert.equal(ws.sent.filter(x=>typeof x==='string'&&JSON.parse(x).type==='speak').length,0);
  await env.call.stop();holding.resolve();
});

test('final without speech_start still interrupts playback and mute ignores late ASR/audio',async()=>{
  const env=environment();await env.call.start();const ws=env.sockets[0];
  ws.emit({type:'speech_start'});ws.emit({type:'final',text:'旧问题',utterance:1});await tick();
  const packet=new ArrayBuffer(12);new DataView(packet).setUint32(0,1,true);ws.emit(packet);
  ws.emit({type:'final',text:'新的问题',utterance:2});await tick();
  assert.equal(env.sources[0].stopped,true);assert.equal(env.turns.length,2);
  ws.emit({type:'final',text:'重复的问题',utterance:2});await tick();assert.equal(env.turns.length,2);
  env.elements.get('#call-mute').click();assert.equal(env.track.enabled,false);
  const sent=ws.sent.length;env.captures[0].port.onmessage({data:new ArrayBuffer(2560)});assert.equal(ws.sent.length,sent);
  ws.emit({type:'speech_start'});ws.emit({type:'final',text:'静音后的迟到识别',utterance:3});ws.emit(packet);await tick();
  assert.equal(env.turns.length,2);assert.equal(env.sources.length,1);
  env.elements.get('#call-mute').click();assert.equal(env.track.enabled,true);
});
