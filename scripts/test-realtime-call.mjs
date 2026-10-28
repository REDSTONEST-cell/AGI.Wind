import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
const source = await readFile(new URL('../app/src/nagi/api/client/call-audio.js', import.meta.url), 'utf8');
const { SpeechChunks, PcmDecoder, PcmPlayer } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('speech chunks start at a clause and flush without losing text', () => {
  const buffer = new SpeechChunks();
  assert.deepEqual(buffer.feed('哥哥我听到了，'), ['哥哥我听到了，']);
  assert.deepEqual(buffer.feed('你接着说'), []);
  assert.deepEqual(buffer.flush(), ['你接着说']);
  assert.deepEqual(buffer.flush(), []);
  assert.deepEqual(new SpeechChunks().feed('甲'.repeat(85)).map(x => x.length), [20,40]);
});

test('short complete replies start immediately and an early comma does not block later clauses', () => {
  assert.deepEqual(new SpeechChunks().feed('听到了。'), ['听到了。']);
  const chunks = new SpeechChunks();
  assert.deepEqual(chunks.feed('嗯，'), []);
  assert.deepEqual(chunks.feed('哥哥我听到了，继续说吧。'), ['嗯，哥哥我听到了，','继续说吧。']);
});

test('microphone sends 40ms frames instead of waiting for 80ms', async () => {
  const source = await readFile(new URL('../app/src/nagi/api/client/call-capture.js', import.meta.url), 'utf8');
  const frames=[]; let Processor;
  class Base { constructor(){this.port={postMessage:buffer=>frames.push(buffer)};} }
  vm.runInNewContext(source, {AudioWorkletProcessor:Base,Float32Array,registerProcessor:(name,value)=>{Processor=value;}});
  const processor=new Processor();
  for(let n=0;n<4;n++)processor.process([[new Float32Array(128).fill(0.25)]]);
  assert.equal(frames.length,0);
  processor.process([[new Float32Array(128).fill(0.25)]]);
  assert.equal(frames.length,1);
  assert.equal(frames[0].byteLength,640*4);
  assert.ok([...new Float32Array(frames[0])].every(value=>value===0.25));
});

test('PCM decoder carries split samples and reset discards stale bytes', () => {
  const decoder = new PcmDecoder();
  assert.deepEqual([...decoder.feed(new Uint8Array([0]))], []);
  assert.deepEqual([...decoder.feed(new Uint8Array([128,255,127]))], [-1,32767/32768]);
  decoder.feed(new Uint8Array([12]));
  decoder.reset();
  assert.deepEqual([...decoder.feed(new Uint8Array([0,0]))], [0]);
});

test('interrupt stops every scheduled audio source and drops queued playback', () => {
  const sources = [];
  const context = {currentTime:1,destination:{},createBuffer:()=>({copyToChannel(){}}),createBufferSource(){
    const source={connect(){},start(at){this.at=at;},stop(){this.stopped=true;},disconnect(){}};
    sources.push(source); return source;
  }};
  const player = new PcmPlayer(context);
  player.push(new Uint8Array(4800),24000);
  player.push(new Uint8Array(4800),24000);
  assert.ok(sources[1].at > sources[0].at);
  player.stop();
  assert.ok(sources.every(x=>x.stopped));
  assert.equal(player.playing,false);
});
