const { test } = require('node:test');
const assert = require('node:assert/strict');
const LY = require('../app/lyrics-lib.js');

test('syllables are counted per word', () => {
  assert.equal(LY.syl('Mona Lisa'), 4);
  assert.equal(LY.syl('smile at me a moment'), 6);
});

test('melody: notes per vocal phrase, ties count once, outro joins the section before it', () => {
  const abc = ['X:1', 'V: Vocal', 'V: Ins', 'K:G', '% verse', 'V: Vocal', 'G2A2B2c2-|c2d2e2f2|', 'V: Ins', 'Z4|',
    'V: Vocal', 'GABc defg|', '% outro', 'V: Vocal', 'ABcd efgg|'].join('\n');
  const m = LY.melody(abc);
  assert.deepEqual(m.map(x => x.name), ['verse', 'outro']);
  assert.equal(LY.countNotes('c2-c2'), 1);
  assert.deepEqual(LY.sung(m).map(x => x.phrases), [[7, 8, 8]]);   // 7 notes after the tie, then 8, then the outro's 8
});

test('fit pairs lyric sections with melody sections in order and reports room', () => {
  const m = [{ name: 'verse', phrases: [8, 8] }, { name: 'chorus', phrases: [4, 4] }];
  const al = LY.align(LY.sections('[verse]\nMona Lisa won\'t you smile\nsmile at me a moment\n\n[chorus]\nMona Lisa Mona Lisa'), m, 1);
  assert.equal(al.length, 2);
  assert.equal(al[0].cap, 16);
  assert.equal(al[1].sylTotal, 8);
  assert.equal(al[1].pct, 1);
});

test('clean up keeps the words; splitting long lines never changes them', () => {
  const raw = '(Verse 1)\nI know you have your reasons, it\'s the time or it\'s the season, it\'s the breath that I\'ve been breathin (oh yeah)\n(chorus)\nI am 5';
  const f = LY.format(raw).text;
  assert.match(f, /^\[Verse\]$/m);
  assert.match(f, /five/);
  const words = t => t.replace(/\[.*?\]/g, '').replace(/,/g, '').split(/\s+/).filter(Boolean).join(' ');
  const parts = LY.splitLine('I know you have your reasons, it\'s the time or it\'s the season, it\'s the breath', 12);
  assert.ok(parts.length > 1);
  assert.equal(words(parts.join(' ')), words('I know you have your reasons, it\'s the time or it\'s the season, it\'s the breath'));
});

test('word diff marks only what changed', () => {
  assert.deepEqual(LY.diff('a b\nc d', 'a x\nc d').filter(x => x[0] !== '=').map(x => x.join('')), ['-b', '+x']);
});
