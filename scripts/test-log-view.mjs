import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../app/src/nagi/api/client/log-view.js', import.meta.url));
const { simpleLogRow, plainTime } = await import('data:text/javascript;base64,' + source.toString('base64'));

test('thinking and replies keep the round/step but lose the technical stage', () => {
  const thinking = simpleLogRow({
    cat: 'proc', tag: 'THINK', level: 'info',
    text: '第 1 轮 · 第 2 步 · 回答前 · 先看看用户刚才说了什么',
  });
  assert.equal(thinking.label, '她在想');
  assert.equal(thinking.text, '第1轮·第2步 · 先看看用户刚才说了什么');
  const reply = simpleLogRow({ cat: 'proc', tag: 'REPLY', level: 'info', text: '回答前 · 回复正文 · 我在呢。' });
  assert.deepEqual({ label: reply.label, text: reply.text }, { label: '她说', text: '回复正文 · 我在呢。' });
});

test('tool names become understandable actions and preserve progress/failure', () => {
  const started = simpleLogRow({ cat: 'proc', tag: 'TOOL', text: '第 1 轮 · 第 1 步 · 回答前 · search_memory · 进行中' });
  assert.equal(started.text, '第1轮·第1步 · 正在翻记忆');
  const done = simpleLogRow({ cat: 'proc', tag: 'TOOL', text: '第 1 轮 · 第 1 步 · 回答前 · web_search · 完成' });
  assert.equal(done.text, '第1轮·第1步 · 上网查了一下');
  const failed = simpleLogRow({ cat: 'proc', tag: 'TOOL', text: '回答前 · send_sticker · 失败' });
  assert.equal(failed.tone, 'error');
  assert.match(failed.text, /没成功/);
});

test('technical system noise is hidden while meaningful events stay', () => {
  assert.equal(simpleLogRow({ cat: 'sys', group: 'api', level: 'info', text: '接口 GET /api/v1/x · 200 · 1ms' }), null);
  assert.equal(simpleLogRow({ cat: 'sys', group: 'gen', level: 'info', text: '开始生成' }), null);
  assert.equal(simpleLogRow({ cat: 'sys', group: 'sys', level: 'info', text: 'uvicorn started' }), null);
  assert.equal(simpleLogRow({ cat: 'sys', group: 'agent', level: 'info', text: 'sticker search query=… hits=2' }), null);
  const memory = simpleLogRow({ cat: 'sys', group: 'memory', level: 'info', text: '记忆提炼 · 事件 2 · 写入 1' });
  assert.deepEqual({ label: memory.label, text: memory.text }, { label: '记忆', text: '整理了一下关于你的记忆' });
  const warning = simpleLogRow({ cat: 'sys', group: 'sys', level: 'warn', text: 'WARNING: gateway is reachable beyond this machine while the host firewall allows it' });
  assert.equal(warning, null);
  const warning2 = simpleLogRow({ cat: 'sys', group: 'sys', level: 'warn', text: 'WARNING: voice queue delayed' });
  assert.equal(warning2.text, 'voice queue delayed');
  const error = simpleLogRow({ cat: 'sys', group: 'api', level: 'error', text: '接口失败' });
  assert.equal(error.label, '出错了');
});

test('client activity becomes a short story; redundant/technical activity is hidden', () => {
  const sent = simpleLogRow({ cat: 'act', text: '发送消息 · 你今天怎么样 · 5 字' });
  assert.deepEqual({ label: sent.label, text: sent.text }, { label: '你', text: '你：你今天怎么样' });
  const proactive = simpleLogRow({ cat: 'act', text: '收到主动消息 · 来自 她' });
  assert.equal(proactive.text, '她主动来找你了');
  assert.equal(simpleLogRow({ cat: 'act', text: '开始流式返回 · 首字 300 ms' }), null);
  assert.equal(simpleLogRow({ cat: 'act', text: '上下文已组装 · 来源 0 条' }), null);
  assert.equal(simpleLogRow({ cat: 'act', text: '上下文已组装 · 来源 3 条 · 人格 v2' }).text, '想起了 3 条相关记忆');
});

test('friendly timestamps avoid raw clock noise for recent rows', () => {
  assert.equal(plainTime(new Date(Date.now() - 5_000)), '刚刚');
  assert.equal(plainTime(new Date(Date.now() - 125_000)), '2 分钟前');
  assert.equal(plainTime('not a date'), '');
});

test('completed turn gets a single human summary', () => {
  const done = simpleLogRow({ cat: 'proc', tag: 'DONE', text: '回复完成 · 2 轮 / 3 步' });
  assert.deepEqual({ label: done.label, text: done.text }, { label: '这一轮', text: '回复完成 · 2 轮 / 3 步' });
  assert.equal(simpleLogRow({ cat: 'proc', tag: 'DONE', text: '她想了想，这次先不打扰' }), null);
  assert.equal(simpleLogRow({ cat: 'proc', tag: 'THINK', text: '独处思考开始 · 先自己想一想' }), null);
  const idleDone = simpleLogRow({ cat: 'proc', tag: 'DONE', text: '她想了想，决定来找你 · 1 轮 / 2 步' });
  assert.match(idleDone.text, /决定来找你/);
});
