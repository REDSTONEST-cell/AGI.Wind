import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source=await readFile(new URL('../app/src/agi_wind/api/client/reply-segments.js',import.meta.url));
const { splitReply }=await import('data:text/javascript;base64,'+source.toString('base64'));
const texts=value=>splitReply(value).map(part=>part.text);

test('sentence punctuation/newlines split bubbles and disappear, commas stay',()=>{
  assert.deepEqual(texts('你好，你来了。真的？太好了！\n我在呢'),['你好，你来了','真的','太好了','我在呢']);
});
test('question and exclamation marks at the beginning survive',()=>{
  assert.deepEqual(texts('？！你认真的？!真的好耶！'),['？！你认真的','!真的好耶']);
  assert.deepEqual(texts('？！'),['？！']);
});
test('punctuation runs/closing quotes do not become orphan bubbles',()=>{
  assert.deepEqual(texts('她说“好呀。”然后呢？！'),['她说“好呀”','然后呢']);
});
test('decimals, URLs and fenced/inline code retain internal punctuation',()=>{
  assert.deepEqual(texts('请打开 app.js。然后修改。'),['请打开 app.js','然后修改']);
  assert.deepEqual(texts('1. 打开文件\n2. 保存'),['1. 打开文件','2. 保存']);
  assert.deepEqual(texts('版本3.14。看 https://example.com/a?q=1!x=2 。好！'),['版本3.14','看 https://example.com/a?q=1!x=2','好']);
  assert.deepEqual(texts('`x?y:z`可以吗？'),['`x?y:z`可以吗']);
  assert.deepEqual(texts('```js\nconsole.log("hi?");\n```\n然后。'),['```js\nconsole.log("hi?");\n```','然后']);
});
test('streaming tails stay visible and chunks produce same final segmentation',()=>{
  assert.deepEqual(splitReply('你好。下一',{final:false}),[{text:'你好',complete:true},{text:'下一',complete:false}]);
  assert.deepEqual(splitReply('3.',{final:false}),[{text:'3.',complete:false}]);
  let text='';for(const chunk of ['你','好。','？你','来啦','！'])text+=chunk;
  assert.deepEqual(texts(text),['你好','？你来啦']);
});
