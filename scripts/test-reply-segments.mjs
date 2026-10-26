import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source=await readFile(new URL('../app/src/agi_wind/api/client/reply-segments.js',import.meta.url));
const { splitReply }=await import('data:text/javascript;base64,'+source.toString('base64'));
const texts=value=>splitReply(value).map(part=>part.text);

test('every model line is one message; trailing punctuation is trimmed',()=>{
  assert.deepEqual(texts('你好，你来了。真的？太好了！\n我在呢'),['你好，你来了。真的？太好了','我在呢']);
  assert.deepEqual(texts('只想发一条就把正文写成一行。'),['只想发一条就把正文写成一行']);
});
test('fenced and inline code keep their punctuation and newlines',()=>{
  assert.deepEqual(texts('```js\nconsole.log("hi?");\n```\n然后。'),['```js\nconsole.log("hi?");\n```','然后']);
  assert.deepEqual(texts('`x?y:z`可以吗？'),['`x?y:z`可以吗']);
});
test('streaming tails stay visible until the line ends',()=>{
  assert.deepEqual(splitReply('你好。\n下一',{final:false}),[{text:'你好',complete:true},{text:'下一',complete:false}]);
  assert.deepEqual(splitReply('3.',{final:false}),[{text:'3.',complete:false}]);
});
