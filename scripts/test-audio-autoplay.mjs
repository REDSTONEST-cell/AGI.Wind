import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../app/src/nagi/api/client/audio.js',import.meta.url),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
async function setup(){
  const module=await import('data:text/javascript;base64,'+Buffer.from(source+'\n// '+Math.random()).toString('base64'));
  class Node{constructor(){this.style={};this.dataset={};this.classList={add(){},toggle(){}};this.listeners={};}setAttribute(){}removeAttribute(){}append(){}remove(){}addEventListener(k,f){this.listeners[k]=f;}}
  globalThis.document={createElement:()=>new Node()};
  const players=[];globalThis.Audio=class extends Node{constructor(url){super();this.url=url;this.paused=true;this.plays=0;players.push(this);}play(){this.paused=false;this.plays++;this.listeners.play?.();return Promise.resolve();}pause(){this.paused=true;this.listeners.pause?.();}load(){}end(){this.ended=true;this.paused=true;this.listeners.ended?.();}};
  const pending=new Map();const client={assetObjectUrl:id=>new Promise(resolve=>pending.set(id,resolve))};
  const create=id=>module.createAudioMessage(client,{bubble:new Node(),body:new Node()},{state:'ready',attachment:{asset_id:id},autoplay:true});
  return {...module,create,pending,players};
}
test('all voice segments autoplay in order, even when later audio loads first',async()=>{
  const e=await setup();e.create('one');e.create('two');
  e.pending.get('two')('two');await tick();assert.equal(e.players[0].plays,0,'later segment must wait');
  e.pending.get('one')('one');await tick();const one=e.players.find(p=>p.url==='one'),two=e.players.find(p=>p.url==='two');
  assert.equal(one.plays,1);assert.equal(two.plays,0);one.end();assert.equal(two.plays,1);two.end();e.stopAllAudio();
});
test('Stop clears queued and still-loading autoplay without late playback',async()=>{
  const e=await setup();e.create('one');e.create('two');e.pending.get('one')('one');await tick();assert.equal(e.players[0].plays,1);
  e.stopAllAudio();e.pending.get('two')('two');await tick();assert.equal(e.players[0].paused,true);assert.equal(e.players[1].plays,0);
});
