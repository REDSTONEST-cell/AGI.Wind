import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source=await readFile(new URL('../app/src/agi_wind/api/client/live-events.js',import.meta.url));
const { LiveCursors, idleCompletion }=await import('data:text/javascript;base64,'+source.toString('base64'));

test('initial snapshot skips old events and reconnect never rewinds',()=>{
  const cursors=new LiveCursors();
  cursors.seed('local:private',40);
  assert.equal(cursors.accept('local:private',39),false);
  assert.equal(cursors.accept('local:private',41),true);
  assert.equal(cursors.accept('local:private',41),false);
  cursors.seed('local:private',0);
  assert.equal(cursors.get('local:private'),41);
  assert.equal(cursors.accept('local:private',42),true);
});

test('conversation and server cursors are independent and bounded',()=>{
  const cursors=new LiveCursors(2);
  cursors.seed('one:private',50); cursors.seed('two:private',1);
  assert.equal(cursors.accept('two:private',2),true);
  assert.equal(cursors.get('one:private'),50);
  cursors.seed('one:group',10);
  assert.equal(cursors.has('one:private'),false);
  assert.equal(cursors.accept('one:group',NaN),false);
});

test('cancellation and quota checks with no model work do not become thought logs',()=>{
  for(const reason of ['user_message','daily_cap','runs_cap','quiet_hours','stopped']){
    assert.equal(idleCompletion({decision:'silent',reason,rounds:0,steps:0}),null);
  }
});

test('real work and failures stay visible with truthful status labels',()=>{
  const silent=idleCompletion({decision:'silent',rounds:1,steps:1});
  assert.match(silent.text,/任务结束/);
  assert.doesNotMatch(silent.text,/她想了想/);
  assert.match(idleCompletion({decision:'reach_out',rounds:1,steps:2}).text,/已发出消息/);
  assert.equal(idleCompletion({decision:'silent',reason:'cognition_timeout',rounds:0}).failed,true);
});
