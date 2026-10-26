// Lyric formatting + melody-fit helpers for the music site editor. Pure functions, no DOM (tested under Node).
(function (root) {
  const ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(' ');
  const TENS = 'x x twenty thirty forty fifty sixty seventy eighty ninety'.split(' ');
  const num = n => n < 20 ? ONES[n] : TENS[n / 10 | 0] + (n % 10 ? '-' + ONES[n % 10] : '');
  const TAGS = { verse: 'Verse', chorus: 'Chorus', refrain: 'Chorus', hook: 'Chorus', bridge: 'Bridge', intro: 'Intro', outro: 'Outro',
    'pre-chorus': 'Pre-Chorus', prechorus: 'Pre-Chorus', 'pre chorus': 'Pre-Chorus', interlude: 'Interlude', solo: 'Solo', instrumental: 'Instrumental' };
  const NOISE = /DRAFT|Correct anything|Delete the|^[A-Z' ]+ - Paul|^\(thunder/i;

  // Rough English syllable count: vowel groups, minus silent e, plus common endings.
  function syl(line) {
    let n = 0;
    for (let w of line.toLowerCase().replace(/[^a-z' -]/g, '').split(/[\s-]+/)) {
      w = w.replace(/'/g, ''); if (!w) continue;
      let c = (w.match(/[aeiouy]+/g) || []).length;
      if (/[^aeiouy]e$/.test(w) && !/([^aeiouy]le|ye)$/.test(w)) c--;
      if (/(ed)$/.test(w) && !/[td]ed$/.test(w)) c--;
      if (/(ia|io[un]|ua|eo|ier)\b/.test(w)) c++;
      n += Math.max(1, c);
    }
    return n;
  }

  // Returns {text, notes[]} -- text is the cleaned lyrics; notes explain every change/warning.
  function format(raw, opt) {
    opt = Object.assign({ maxSyl: 99, split: false }, opt);
    const notes = [];
    let t = raw.replace(/\r/g, '').replace(/[\u2018\u2019]/g, "'").replace(/[\u201c\u201d]/g, '"').replace(/[\u2013\u2014]/g, ', ');
    const out = []; let sec = null;
    const push = l => { if (sec) sec.lines.push(l); else { sec = { tag: null, lines: [l] }; out.push(sec); } };
    for (let l of t.split('\n')) {
      l = l.trim();
      if (!l) continue;
      if (NOISE.test(l)) { notes.push('dropped draft/noise line: ' + l.slice(0, 40)); continue; }
      const m = l.match(/^[\[(]?\s*(pre[- ]?chorus|verse|chorus|refrain|hook|bridge|intro|outro|interlude|solo|instrumental)\b[^\])\n]{0,12}[\])]?\s*:?$/i);
      if (m) { sec = { tag: TAGS[m[1].toLowerCase().replace('-', ' ')] || TAGS[m[1].toLowerCase()], lines: [] }; out.push(sec); continue; }
      l = l.replace(/[\[\]()]/g, '');                       // backing-vocal brackets can't be sung separately
      l = l.replace(/\b\d{1,2}\b/g, d => (notes.push('spelled out number ' + d), num(+d)));
      l = l.replace(/&/g, 'and').replace(/\s{2,}/g, ' ').replace(/[,;:\s]+$/, '').trim();
      if (!l) continue;
      if (opt.split && syl(l) > opt.maxSyl) {
        const parts = splitLine(l, opt.maxSyl);
        if (parts.length > 1) notes.push(`split a ${syl(l)}-syllable line into ${parts.length}: "${l.slice(0, 30)}..."`);
        parts.forEach(push);
      } else push(l);
    }
    if (!out.some(s => s.tag)) notes.push('no section tags found: YuE needs [Verse]/[Chorus] markers');
    const text = out.filter(s => s.lines.length).map(s => (s.tag ? `[${s.tag}]\n` : '') + s.lines.join('\n')).join('\n\n') + '\n';
    return { text, notes };
  }

  function splitLine(l, max) {
    // best cut = strong punctuation / run-on capital / comma near the middle; falls back to conjunctions, then any space
    if (syl(l) <= max) return [l];
    const BS = String.fromCharCode(92), cands = [];
    const add = (re, bonus) => { for (const m of l.matchAll(re)) cands.push([m.index, m.index + m[0].length, bonus]); };
    add(new RegExp('[.?!]' + BS + 's+', 'g'), 14);
    add(new RegExp(',' + BS + 's*', 'g'), 8);
    add(new RegExp(BS + 's+(?=(?!(?:I|I' + BS + "'" + '[a-z]+|Mona|Lisa|God)' + BS + 'b)[A-Z])', 'g'), 12);
    add(new RegExp(BS + 's+(?=(and|but|or|so|cause|because|when|while|that|if|just|why)' + BS + 'b)', 'gi'), 3);
    if (!cands.length) add(new RegExp(BS + 's+', 'g'), 0);
    const mid = l.length / 2;
    cands.sort((x, y) => (Math.abs(x[0] - mid) - x[2]) - (Math.abs(y[0] - mid) - y[2]));
    for (const [a, b] of cands) {
      const L = l.slice(0, a + (/^[.?!,]/.test(l.slice(a)) ? 1 : 0)).trim(), R = l.slice(b).trim();
      if (L && R) return [...splitLine(L, max), ...splitLine(R, max)];
    }
    return [l];
  }

  // Parse lyrics text into [{tag, lines[]}] (tag null if absent).
  function sections(text) {
    const out = []; let s = null;
    for (const l of text.replace(/\r/g, '').split('\n')) {
      const t = l.trim(); if (!t) continue;
      const m = t.match(/^\[(.+)\]$/);
      if (m) { s = { tag: m[1], lines: [] }; out.push(s); } else { if (!s) out.push(s = { tag: null, lines: [] }); s.lines.push(t); }
    }
    return out.filter(x => x.lines.length);
  }

  // cover.abc -> [{name, phrases:[noteCount,...]}] for the Vocal voice. One vocal block between voice switches = one phrase.
  function melody(abc) {
    const secs = []; let name = 'start', voice = null, cur = null, block = null;
    const flush = () => { if (block && block.n > 0) { let s = secs[secs.length - 1]; if (!s || s.name !== block.sec) secs.push(s = { name: block.sec, phrases: [] }); s.phrases.push(block.n); } block = null; };
    for (let l of abc.replace(/\r/g, '').split('\n')) {
      if (/^%\s*\S/.test(l)) { flush(); name = l.replace(/^%\s*/, '').trim().toLowerCase(); continue; }
      const v = l.match(/^V:\s*(\w+)/);
      if (v) { if (voice !== v[1]) flush(); voice = v[1]; if (voice === 'Vocal') block = block || { n: 0, sec: name }; continue; }
      if (/^[A-Z]:/.test(l) || voice !== 'Vocal') continue;
      if (!block) block = { n: 0, sec: name };
      block.n += countNotes(l);
    }
    flush();
    return secs;
  }

  function countNotes(line) {
    line = line.replace(/"[^"]*"/g, '').replace(/![^!]*!/g, '').replace(/\{[^}]*\}/g, '');
    let n = 0, tied = null;
    for (const m of line.matchAll(/([_^=]*)([A-Ga-g])([,']*)[\d\/]*(-)?/g)) {
      const p = m[2] + m[3];
      if (tied !== p) n++;
      tied = m[4] ? p : null;
    }
    return n;
  }

  // Sung melody sections: drop lead-ins (<8 notes) and merge an outro into the section before it.
  function sung(msecs) {
    const out = [];
    for (const m of msecs) {
      if (m.phrases.reduce((a, b) => a + b, 0) < 8) continue;
      if (m.name === 'outro' && out.length) out[out.length - 1] = { name: out[out.length - 1].name, phrases: out[out.length - 1].phrases.concat(m.phrases) };
      else out.push(m);
    }
    return out;
  }

  // Pair lyric sections with the melody's sung sections in order.
  // r = melody notes per syllable. Each pair gets capacity (syllables the melody can carry) and a per-line phrase assignment.
  function align(lsecs, msecs, r) {
    r = r || 1;
    const ms = sung(msecs), out = [];
    for (let i = 0; i < Math.max(lsecs.length, ms.length); i++) {
      const L = lsecs[i], M = ms[i], phrases = M ? M.phrases : [];
      const lines = (L ? L.lines : []).map(line => ({ line, syl: syl(line) }));
      const sylTotal = lines.reduce((a, x) => a + x.syl, 0), cap = phrases.reduce((a, b) => a + b, 0) / r;
      let acc = 0, bound = [], c = 0; phrases.forEach(p => bound.push(c += p / r));
      for (const x of lines) { const mid = acc + x.syl / 2; acc += x.syl; const j = bound.findIndex(bb => bb >= mid - 0.01); x.phrase = j; }
      out.push({ lyric: L ? L.tag : null, mel: M ? M.name : null, lines, phrases, sylTotal, cap, pct: cap ? sylTotal / cap : null });
    }
    return out;
  }

  // Fit a section: split any line longer than perLine syllables at natural breaks (sentence ends, commas, run-on capitals, conjunctions).
  // Never merges lines or changes words; the melody (phrases, r) is only used by the UI to show room per phrase.
  function fit(lines, phrases, r, perLine) {
    return lines.flatMap(l => splitLine(l, perLine || 16));
  }

  // Word-level diff: returns [[op, token]] with op '=', '-', '+'. Newlines are tokens.
  function diff(a, b) {
    const tok = t => t.split(/(\n)|\s+/).filter(x => x !== undefined && x !== '');
    const A = tok(a), B = tok(b), n = A.length, m = B.length;
    const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    const out = []; let i = 0, j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { out.push(['=', A[i]]); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(['-', A[i++]]); else out.push(['+', B[j++]]);
    }
    while (i < n) out.push(['-', A[i++]]); while (j < m) out.push(['+', B[j++]]);
    return out;
  }

  const api = { syl, format, sections, melody, sung, countNotes, align, fit, splitLine, diff };
  if (typeof module !== 'undefined') module.exports = api; else root.LY = api;
})(this);
