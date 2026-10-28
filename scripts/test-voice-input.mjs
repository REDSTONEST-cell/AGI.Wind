import test from 'node:test';
import assert from 'node:assert/strict';
const { microphoneConstraints, microphoneError } = await import('../app/src/nagi/api/client/microphone.js');
const { createVoiceInput } = await import('../app/src/nagi/api/client/voice-input.js');
const tick = () => new Promise(r=>setImmediate(r));

test('microphone uses explicit system default rather than browser virtual preference',()=>{
  const devices=[{kind:'audioinput',deviceId:'steam'},{kind:'audioinput',deviceId:'default'},{kind:'audioinput',deviceId:'physical'}];
  assert.deepEqual(microphoneConstraints(devices).audio.deviceId,{exact:'default'});
  assert.deepEqual(microphoneConstraints(devices,'physical').audio.deviceId,{exact:'physical'});
  assert.deepEqual(microphoneConstraints(devices,'removed').audio.deviceId,{exact:'default'});
  assert.match(microphoneError({name:'NotFoundError'}),/没有找到/);
  assert.match(microphoneError({name:'NotAllowedError'}),/权限/);
});
function environment({permission}={}){
  globalThis.location={href:'http://localhost:18440/'};
  const tracks=[{stopped:false,stop(){this.stopped=true;}}];
  const stream={getTracks:()=>tracks,getAudioTracks:()=>tracks};
  const contexts=[];globalThis.AudioContext=class{constructor(){this.sampleRate=16000;this.audioWorklet={addModule:async()=>{}};this.destination={};contexts.push(this);}async resume(){}async close(){this.closed=true;}createMediaStreamSource(){return {connect(){},disconnect(){}};}};
  globalThis.AudioWorkletNode=class{constructor(){this.port={};}connect(){}disconnect(){}};
  const sockets=[];globalThis.WebSocket=class{static OPEN=1;constructor(){this.readyState=1;this.bufferedAmount=0;this.sent=[];sockets.push(this);queueMicrotask(()=>this.onopen?.());}send(x){this.sent.push(x);if(typeof x==='string'&&JSON.parse(x).ticket)queueMicrotask(()=>this.emit({type:'ready'}));}emit(x){this.onmessage?.({data:JSON.stringify(x)});}close(){this.readyState=3;this.onclose?.();}};
  let current='one';const text=[],errors=[],states=[];const requests=[];
  const client={url:x=>x,async request(path,options){requests.push(path);return {ticket:'synthetic'};},chat(){assert.fail('dictation must never send a chat request');}};
  const input=createVoiceInput(client,{prepare:async()=>current,conversationId:()=>current,openMicrophone:()=>permission||Promise.resolve(stream),text:x=>text.push(x),state:(...x)=>states.push(x),error:x=>errors.push(x)});
  return {input,text,errors,states,sockets,tracks,contexts,requests,stream,switch:()=>{current='two';}};
}
test('voice input streams text only, deduplicates finals and releases resources on cancel',async()=>{
  const e=environment();await e.input.start();const ws=e.sockets[0];
  ws.emit({type:'partial',text:'你好'});assert.equal(e.text.at(-1),'你好');
  ws.emit({type:'final',text:'你好呀',utterance:1});ws.emit({type:'final',text:'重复',utterance:1});
  ws.emit({type:'partial',text:'今天'});assert.equal(e.text.at(-1),'你好呀 今天');
  ws.emit({type:'final',text:'今天好吗',utterance:2});assert.equal(e.text.at(-1),'你好呀 今天好吗');
  await e.input.cancel();assert.ok(e.tracks.every(t=>t.stopped));assert.ok(e.contexts.every(c=>c.closed));assert.equal(e.input.active,false);
  ws.emit({type:'partial',text:'迟到'});assert.equal(e.text.at(-1),'你好呀 今天好吗');
  assert.deepEqual(e.requests,['/api/v1/call/ticket']);
});
test('switching conversation discards late recognition and cancels capture',async()=>{
  const e=environment();await e.input.start();e.switch();e.sockets[0].emit({type:'partial',text:'旧窗口文字'});await tick();
  assert.deepEqual(e.text,[]);assert.equal(e.input.active,false);assert.ok(e.tracks.every(t=>t.stopped));
});
test('finish releases microphone immediately and accepts the final ASR correction before closing',async()=>{
  const e=environment();await e.input.start();const ws=e.sockets[0];
  ws.emit({type:'partial',text:'雨与'});await e.input.finish();
  assert.ok(e.tracks.every(t=>t.stopped));assert.equal(e.input.active,true);
  ws.emit({type:'final',text:'雨雨',utterance:1});
  await new Promise(r=>setTimeout(r,1500));
  assert.equal(e.text.at(-1),'雨雨');assert.equal(e.input.active,false);
  assert.equal(ws.readyState,3);
});
test('a late microphone permission grant after cancel is released',async()=>{
  let resolve;const promise=new Promise(r=>{resolve=r;});const e=environment({permission:promise});const starting=e.input.start();await tick();await e.input.cancel();resolve(e.stream);await starting;
  assert.ok(e.tracks.every(t=>t.stopped));assert.equal(e.sockets.length,0);assert.equal(e.input.active,false);
});
