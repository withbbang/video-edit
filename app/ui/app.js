'use strict';
/* youtube-edit 화면. 파이썬 Api 는 pywebview.api.*, 파이썬 알림은 window.onAppEvent(event, data). */

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

const OPTS = [
  { key: 'filler', label: '망설임 소리', sub: '아·어·음…', choices: [['keep', '유지'], ['remove', '제거']] },
  { key: 'habit', label: '말버릇', sub: '그니까·약간·이제…', choices: [['keep', '유지'], ['some', '일부'], ['all', '모두']] },
  { key: 'silence', label: '말 없는 구간', slider: true, ends: ['안 자름', '최대한 자름'], say: silenceSay },
  { key: 'denoise', label: '배경 잡음', slider: true, ends: ['끔', '최대 (목소리 먹먹해질 수 있음)'], say: denoiseSay },
];
const ADV = [
  { key: 'loudnorm', label: '음량 맞추기', sub: '유튜브 기준 크기(-14 LUFS)로 맞춤', choices: [['keep', '유지'], ['match', '맞춤']] },
  { key: 'subs', label: '영상 안 자막', sub: 'mp4 안에 켜고 끄는 자막 트랙. SRT 파일은 항상 만듦', choices: [['omit', '빼기'], ['embed', '넣기']] },
  { key: 'quality', label: '화질', sub: '자동: 1080p30 8Mbps · 4K30 약 32Mbps', choices: [['low', '작게'], ['auto', '자동'], ['high', '높게']] },
];
const DEFAULTS = { filler: 'remove', habit: 'some', silence: 50, denoise: 50, loudnorm: 'match', subs: 'embed', quality: 'auto' };
const FAST = new Set(['filler', 'habit', 'silence', 'denoise']);

// 슬라이더 설명 한 줄. st: 지금 결과의 말 없는 구간 {곳, 초} (결과가 없으면 null)
function silenceSay(v, st) {
  const tail = st ? ` · ${st.n}곳 · −${st.sec}초` : '';
  if (v <= 0) return '말 없는 구간을 자르지 않습니다';
  if (v <= 50) return `조용히 말 없는 구간 중 ${(1.0 - 0.7 * v / 50).toFixed(1)}초 이상만 자름${tail}`;
  if (v < 100) return `조금 시끄러운 말 없는 구간도 자름 (0.3초 이상)${tail}`;
  return `말 없는 구간은 소리가 있어도 전부 자름${tail}`;
}
function denoiseSay(v) {
  if (v <= 0) return '원래 소리 그대로';
  return v <= 50 ? '목소리를 살리며 가볍게 지움' : '계속 깔리는 소리·튀는 소리까지 세게 지움';
}
const BUSY = new Set(['processing', 'saving']);
const HAS_RESULT = new Set(['review', 'saved']);
const ROWS_FIRST = 5;

const S = {
  jobs: [], sel: null, options: null,
  result: null, resultFor: null, resultKey: null,
  showAll: false, showAllCuts: false, changedKey: null, saveDir: '', advOpen: false, update: null,
  resultVersion: 0, drawn: null,
};

// ---- 작은 도우미 ---------------------------------------------------------
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'style') e.style.cssText = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k);
  return e;
}
const pct = (p) => Math.floor(Math.max(0, Math.min(1, Number(p) || 0)) * 100);
function clock(sec) {
  let s = Math.max(0, Math.round(Number(sec) || 0));
  const hh = Math.floor(s / 3600); s -= hh * 3600;
  const mm = Math.floor(s / 60), ss = s % 60;
  const two = (n) => String(n).padStart(2, '0');
  return hh ? `${hh}:${two(mm)}:${two(ss)}` : `${two(mm)}:${two(ss)}`;
}
const selected = () => S.jobs.find((j) => j.id === S.sel) || null;
async function call(name, ...args) {
  try { return await api()[name](...args); } catch (e) { console.error(name, e); return undefined; }
}

function statusText(j) {
  switch (j.status) {
    case 'waiting': return '대기';
    case 'processing': return `처리 중 ${pct(j.progress)}%`;
    case 'review': return '확인 대기';
    case 'saving': return `저장 중 ${pct(j.progress)}%`;
    case 'saved': return '✓ 저장됨';
    case 'failed': return '실패';
    case 'missing': return '원본 없음';
    default: return j.status;
  }
}

// ---- 목록 ----------------------------------------------------------------
let refreshing = false, refreshAgain = false;
async function refresh() {
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try {
    do {
      refreshAgain = false;
      const jobs = await call('list_jobs');
      if (Array.isArray(jobs)) applyJobs(jobs);
    } while (refreshAgain);
  } finally { refreshing = false; }
}

function applyJobs(jobs) {
  const before = new Set(S.jobs.map((j) => j.id));
  S.jobs = jobs;
  if (S.sel && !jobs.some((j) => j.id === S.sel)) select(null);
  if (!S.sel && jobs.length) select((jobs.find((j) => !before.has(j.id)) || jobs[0]).id);
  syncResult();
  render();
}

function select(id) {
  if (S.sel === id) return;
  S.sel = id; S.options = null; S.result = null; S.resultFor = null; S.resultKey = null;
  S.showAll = false; S.showAllCuts = false; S.changedKey = null;
  player.detach();
  if (id) {
    call('get_options', id).then((o) => { if (S.sel === id && o) { S.options = o; render(); } });
    syncResult();
  }
  render();
}

// 선택한 영상의 결과는 상태·요약이 바뀌었을 때만 다시 읽는다 ("job" 알림은 신호일 뿐)
function syncResult() {
  const j = selected();
  if (!j) return;
  if (HAS_RESULT.has(j.status)) {
    const key = j.status + JSON.stringify(j.summary);
    if (key !== S.resultKey) { S.resultKey = key; loadResult(j.id); }
  } else {
    S.resultKey = null;
    if (!BUSY.has(j.status)) { S.result = null; S.resultFor = null; player.detach(); }
  }
}

let resultSeq = 0;
async function loadResult(id) {
  const seq = ++resultSeq;
  const r = await call('get_result', id);
  if (seq !== resultSeq || S.sel !== id) return;   // 늦게 온 옛 응답은 버린다
  if (!r) { S.resultKey = null; return; }          // 다음 알림 때 다시 읽는다
  S.result = r; S.resultFor = id; S.resultVersion++;
  if (!r.changes) S.changedKey = null;
  render();
}

// ---- 그리기 --------------------------------------------------------------
function render() {
  renderQueue();
  renderOptions();
  renderPrimary();
  renderResult();
}

// 목록 줄은 영상 id 마다 한 번 만들고 진행 알림 때는 글자·막대만 고친다 (누르는 도중 다시 만들면 클릭이 사라진다)
const rows = new Map();
const addBox = h('div', { class: 'qi add', onclick: () => pickFiles() }, '＋ 끌어다 놓거나 ', h('u', {}, '파일 선택'));

function queueRow(id) {
  const r = { name: h('span', { class: 'name' }), st: h('span', { class: 'st' }), bar: h('i') };
  r.mini = h('span', { class: 'mini' }, r.bar);
  r.x = h('button', { type: 'button', class: 'x', title: '빼기', onclick: (e) => {
    e.stopPropagation();
    const j = S.jobs.find((x) => x.id === id);
    if (j) removeJob(j);
  } }, '✕');
  r.el = h('div', { class: 'qi', onclick: () => select(id) }, r.name, r.st, r.mini, r.x);
  return r;
}

function renderQueue() {
  const empty = S.jobs.length === 0;
  $('empty').hidden = !empty;
  $('work').hidden = empty;
  const ids = new Set(S.jobs.map((j) => j.id));
  for (const id of [...rows.keys()]) if (!ids.has(id)) { rows.get(id).el.remove(); rows.delete(id); }
  for (const j of S.jobs) {
    if (!rows.has(j.id)) rows.set(j.id, queueRow(j.id));
    const r = rows.get(j.id);
    r.el.classList.toggle('cur', j.id === S.sel);
    r.el.title = j.name;
    if (r.name.textContent !== j.name) r.name.textContent = j.name;
    const text = statusText(j);
    if (r.st.textContent !== text) r.st.textContent = text;
    r.st.className = `st ${j.status}`;
    r.st.title = (j.status === 'failed' || j.status === 'review') ? (j.error || '') : '';
    r.mini.hidden = !BUSY.has(j.status);
    r.bar.style.transform = `scaleX(${pct(j.progress) / 100})`;
  }
  // 영상이 늘거나 줄었을 때만 순서를 맞춘다
  const q = $('queue');
  const want = [...S.jobs.map((j) => rows.get(j.id).el), addBox];
  if (q.children.length !== want.length || want.some((e, i) => q.children[i] !== e)) q.replaceChildren(...want);
}

function segRow(def, value, disabled, onPick, changed) {
  return h('div', { class: 'opt' + (disabled ? ' disabled' : '') + (changed ? ' changed' : '') },
    h('span', {}, def.label, def.sub ? h('small', {}, def.sub) : null),
    h('span', { class: 'seg' }, def.choices.map(([v, text]) =>
      h('button', { type: 'button', class: v === value ? 'sel' : null, disabled: disabled || null,
        onclick: () => onPick(def.key, v) }, text))));
}

function silenceStats() {
  const j = selected();
  const s = j && S.resultFor === j.id && S.result && S.result.summary;
  if (!s || typeof s.silence_cuts !== 'number') return null;
  return { n: s.silence_cuts, sec: Math.round(Number(s.silence_sec) || 0) };
}

// 0~100 슬라이더: 끄는 동안(input)엔 숫자·설명만, 손을 떼면(change) 옵션을 바꾼다
let sliderShows = [];
function sliderRow(def, value, disabled, _onPick, changed) {
  const num = h('span', { class: 'val' });
  const say = h('div', { class: 'say' });
  const input = h('input', { type: 'range', min: 0, max: 100, step: 1, value, disabled: disabled || null, 'aria-label': def.label });
  const show = () => {
    num.textContent = input.value;
    const t = def.say(Number(input.value), silenceStats());
    if (say.textContent !== t) say.textContent = t;
  };
  input.addEventListener('input', show);
  input.addEventListener('change', () => { show(); setOption(def.key, Number(input.value)); });
  sliderShows.push(show);
  show();
  return h('div', { class: 'opt slider' + (disabled ? ' disabled' : '') + (changed ? ' changed' : '') },
    h('div', { class: 'top' }, h('span', {}, def.label), num),
    input,
    h('div', { class: 'ends' }, h('span', {}, def.ends[0]), h('span', {}, def.ends[1])),
    say);
}

// 선택한 영상·옵션·바쁜 상태 등이 바뀔 때만 다시 만든다 (슬라이더 설명의 곳·초는 결과가 바뀌면 그 자리에서 고친다)
let optionsDrawn = null;
function renderOptions(force) {
  const j = selected();
  const o = S.options || DEFAULTS;
  const disabled = !j || !S.options || BUSY.has(j.status) || j.status === 'missing';
  const key = JSON.stringify([j && j.id, j && j.name, o, disabled, S.changedKey, S.advOpen, S.saveDir]);
  if (!force && key === optionsDrawn) { for (const show of sliderShows) show(); return; }
  optionsDrawn = key;
  $('opt-title').textContent = j ? `${j.name} 옵션` : '';
  sliderShows = [];
  $('options').replaceChildren(...OPTS.map((d) => (d.slider ? sliderRow : segRow)(d, o[d.key], disabled, setOption, S.changedKey === d.key)));
  $('adv-arrow').textContent = S.advOpen ? '▾' : '▸';
  $('adv').hidden = !S.advOpen;
  $('adv').replaceChildren(
    ...ADV.map((d) => segRow(d, o[d.key], disabled, setOption, false)),
    h('div', { class: 'opt' },
      h('span', {}, '저장 위치', h('small', { class: 'path' }, S.saveDir)),
      h('span', { class: 'seg' }, h('button', { type: 'button', onclick: setSaveDir }, '설정'))));
}

function renderPrimary() {
  const b = $('primary');
  const j = selected();
  if (j && HAS_RESULT.has(j.status)) {
    b.textContent = '저장';
    b.disabled = false;
  } else {
    b.textContent = '시작';
    b.disabled = !S.jobs.some((x) => x.status === 'waiting' || x.status === 'failed');
  }
}

// 진행률 알림마다 결과를 다시 그리지 않는다 (상태·결과가 바뀔 때만; force 는 펼치기 등)
function renderResult(force) {
  const box = $('result');
  const j = selected();
  const key = j ? [j.id, j.status, j.error, S.resultVersion, S.showAll, S.showAllCuts].join('|') : '';
  if (!force && key === S.drawn) return;
  S.drawn = key;
  if (!j) { box.replaceChildren(); return; }
  const r = S.resultFor === j.id ? S.result : null;
  box.classList.toggle('busy', !!r && BUSY.has(j.status));
  const parts = [];
  if (j.status === 'failed') {
    parts.push(h('div', { class: 'err' }, '실패' + (j.error ? ` — ${j.error}` : '')));
  } else if (j.status === 'review' && j.error) {
    // 저장 실패는 결과를 그대로 두고 이유만 보여 준다 (저장을 바로 다시 누를 수 있다)
    parts.push(h('div', { class: 'err' }, `저장 실패 — ${j.error}`));
  }
  if (r) parts.push(...resultParts(j, r));
  // 재생기는 그대로 두고 나머지만 다시 그린다 (재생 위치·목록 스크롤 위치 유지)
  const oldList = box.querySelector('.cut-list');
  const listTop = oldList ? oldList.scrollTop : 0;
  box.replaceChildren(...parts);
  const newList = box.querySelector('.cut-list');
  if (newList) newList.scrollTop = listTop;
  if (r && r.preview_url) player.attach(r);
  else if (r) player.detach();
}

function resultParts(j, r) {
  const s = r.summary || {};
  const out = [];
  out.push(h('div', { class: 'summary' },
    h('b', { class: 'stat' }, `${clock(s.orig_sec)} → ${clock(s.final_sec)}`), ' · ',
    h('span', { class: 'stat' }, `${s.cuts || 0}곳 잘림`), ' · ',
    h('span', { class: 'stat' }, `−${Math.round(Number(s.removed_sec) || 0)}초`),
    s.restored ? [' · ', h('span', { class: 'stat back' }, `${s.restored}곳 되살림`)] : []));
  // 미리보기를 못 만들어도 컷·요약은 그대로 쓸 수 있다
  out.push(r.preview_url ? player.slot() : h('div', { class: 'err' }, '미리보기를 만들지 못했습니다'));
  out.push(timeline(r));
  out.push(h('div', { class: 'legend' },
    h('b', { class: 'cut' }, '■ 잘린 곳'), ' · ', h('b', { class: 'changed' }, '■ 달라진 곳'),
    s.restored ? [' · ', h('b', { class: 'back' }, '▭ 되살린 곳')] : []));
  if (r.changes) out.push(...changeRows(r.changes));
  out.push(...cutRows(j, r));
  if (j.status === 'saved' && r.saved_paths && r.saved_paths.length) {
    out.push(h('div', { class: 'saved-files' }, r.saved_paths.map((p) => h('div', {}, p))));
  }
  return out;
}

function timeline(r) {
  const dur = r.duration || player.duration() || 0;
  const bar = h('div', { class: 'tl', onclick: (e) => {
    const rect = bar.getBoundingClientRect();
    const d = r.duration || player.duration();
    if (d) player.seek(((e.clientX - rect.left) / rect.width) * d);
  } });
  if (dur > 0) {
    const pos = (a, b) => `left:${(a / dur) * 100}%;width:${((b - a) / dur) * 100}%`;
    for (const [a, b] of r.cuts || []) bar.append(h('b', { style: pos(a, b) }));
    for (const c of r.cut_items || []) if (c.restored) bar.append(h('u', { style: pos(c.start, c.end) }));
    for (const row of (r.changes && r.changes.rows) || []) bar.append(h('s', { style: pos(row.start, row.end) }));
  }
  bar.append(player.head);
  return bar;
}

function changeRows(ch) {
  const out = [];
  const sil = ch.silence || {};
  if (sil.added) out.push(h('div', { class: 'opt' }, h('span', {}, `말 없는 구간 ${sil.added}곳 `, h('span', { class: 'tag add' }, '추가 잘림')), h('span')));
  if (sil.restored) out.push(h('div', { class: 'opt' }, h('span', {}, `말 없는 구간 ${sil.restored}곳 `, h('span', { class: 'tag back' }, '다시 살림')), h('span')));
  const rows = ch.rows || [];
  const shown = S.showAll ? rows : rows.slice(0, ROWS_FIRST);
  for (const row of shown) {
    const added = row.kind === 'added';
    // 줄을 누르면 그 자리 조금 앞부터 지금 모습(잘린 곳은 건너뛰며)으로 들려 준다
    out.push(h('div', { class: 'opt row', onclick: () => player.clip(row.start, row.end, true) },
      h('span', {}, `${clock(row.start)} “${row.word}” `,
        h('span', { class: added ? 'tag add' : 'tag back' }, added ? '추가 잘림' : '다시 살림')),
      h('span')));
  }
  if (!S.showAll && rows.length > ROWS_FIRST) {
    out.push(h('div', { class: 'opt more', onclick: () => { S.showAll = true; renderResult(true); } }, h('span', {}, '+ 더 보기'), h('span')));
  }
  return out;
}

const KIND = { silence: '말 없는 구간', filler: '망설임 소리', habit: '말버릇' };

// 잘린 곳 목록: 하나씩 되살리거나 다시 자를 수 있다 (처리 없이 바로 반영, 저장 때 쓰임)
function cutRows(j, r) {
  const items = r.cut_items || [];
  if (!items.length) return [];
  const n = items.filter((c) => c.restored).length;
  const rows = [];
  const shown = S.showAllCuts ? items : items.slice(0, ROWS_FIRST);
  const editable = HAS_RESULT.has(j.status);
  for (const c of shown) {
    const name = c.kind === 'silence' || !c.word ? KIND[c.kind] || '' : `“${c.word}”`;
    const sub = (c.kind === 'silence' || !c.word ? '' : `${KIND[c.kind] || ''} · `) + `${(c.end - c.start).toFixed(1)}초`;
    rows.push(h('div', { class: 'opt row' + (c.restored ? ' undone' : ''), onclick: () => player.clip(c.start, c.end, true) },
      h('span', {}, h('span', { class: 'w' }, `${clock(c.start)} ${name}`), h('span', { class: 'sec' }, sub)),
      h('button', { type: 'button', class: 'btn' + (c.restored ? ' on' : ''), disabled: !editable,
        onclick: (e) => { e.stopPropagation(); setRestored(j.id, c, !c.restored); } },
        c.restored ? '다시 자르기' : '되살리기')));
  }
  if (!S.showAllCuts && items.length > ROWS_FIRST) {
    rows.push(h('div', { class: 'opt more', onclick: () => { S.showAllCuts = true; renderResult(true); } }, h('span', {}, '+ 더 보기'), h('span')));
  }
  // 목록만 따로 스크롤한다 (재생기·타임라인은 그대로 보이게)
  return [h('h3', { class: 'list-title' }, `잘린 곳 ${items.length}` + (n ? ` · 되살림 ${n}` : '')),
    h('div', { class: 'cut-list' }, rows)];
}

async function setRestored(id, c, on) {
  const r = await call('set_restored', id, c.start, c.end, on);
  if (r) loadResult(id);
}

// ---- 미리보기 재생기: 잘린 구간을 건너뛰며 재생 ---------------------------
const player = {
  video: null, url: null, ranges: [], win: null, raf: 0, ownSeek: false,
  head: h('i', { class: 'head', style: 'left:0' }),

  slot() {
    if (!this.video) {
      const v = h('video', { controls: true, preload: 'metadata', playsinline: true });
      v.addEventListener('timeupdate', () => this.tick());
      v.addEventListener('play', () => this.loop());
      v.addEventListener('seeked', () => this.drawHead());
      v.addEventListener('seeking', () => {
        if (!this.ownSeek) this.win = null;   // 사용자가 직접 옮기면 들어보기 구간을 끝낸다
        this.ownSeek = false;
      });
      v.addEventListener('loadedmetadata', () => { if (!(S.result && S.result.duration)) renderResult(true); });
      this.video = v;
    }
    return this.video;
  },
  attach(r) {
    const v = this.slot();
    if (this.url !== r.preview_url) {
      // 다시 만든 미리보기(잡음 세기 등)는 듣던 자리에서 이어서 재생한다
      const keep = this.url ? { t: v.currentTime, play: !v.paused } : null;
      this.url = r.preview_url; v.src = r.preview_url;
      if (keep) {
        v.addEventListener('loadedmetadata', () => {
          if (this.video !== v || this.url !== r.preview_url) return;
          if (keep.t > 0) this.move(Math.min(keep.t, this.duration() || keep.t));
          if (keep.play) v.play().catch(() => {});
        }, { once: true });
      }
    }
    this.ranges = merge(r.cuts || []);
    this.drawHead();
  },
  detach() {
    if (this.video) { this.video.pause(); this.video.removeAttribute('src'); this.video.load(); }
    this.video = null; this.url = null; this.ranges = []; this.win = null;
  },
  duration() { return this.video && isFinite(this.video.duration) ? this.video.duration : 0; },
  move(t) { this.ownSeek = true; this.video.currentTime = t; },
  seek(t) { if (!this.video) return; this.win = null; this.move(Math.max(0, t)); },
  clip(start, end, skip) {
    const v = this.video;
    if (!v) return;
    const from = Math.max(0, start - 2);
    const to = Math.min(this.duration() || end + 2, end + 2);
    this.win = { to, skip };
    this.move(from);
    v.play().catch(() => {});
  },
  loop() {
    cancelAnimationFrame(this.raf);
    const step = () => {
      this.tick();
      if (this.video && !this.video.paused && !this.video.ended) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  },
  tick() {
    const v = this.video;
    if (!v) return;
    this.drawHead();
    if (v.paused) return;
    const t = v.currentTime;
    if (this.win) {
      if (t >= this.win.to) { v.pause(); this.win = null; return; }
      if (!this.win.skip) return;
    }
    for (const [a, b] of this.ranges) {
      if (a > t) break;
      if (t >= a && t < b - 0.01) { this.move(Math.min(b, this.duration() || b)); break; }
    }
  },
  drawHead() {
    const d = (S.result && S.result.duration) || this.duration();
    const t = this.video ? this.video.currentTime : 0;
    this.head.style.left = d ? `${Math.min(100, (t / d) * 100)}%` : '0';
  },
};

function merge(cuts) {
  const out = [];
  for (const [a, b] of [...cuts].sort((x, y) => x[0] - y[0])) {
    const last = out[out.length - 1];
    if (last && a <= last[1] + 0.02) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out;
}

// ---- 동작 ----------------------------------------------------------------
async function pickFiles() {
  const jobs = await call('pick_files');
  if (Array.isArray(jobs)) applyJobs(jobs);
}

async function removeJob(j) {
  if (BUSY.has(j.status)) {
    const what = j.status === 'saving' ? '저장' : '처리';
    if (!window.confirm(`${j.name}\n${what}를 멈추고 목록에서 뺄까요?`)) return;
  }
  const jobs = await call('remove_job', j.id);
  if (Array.isArray(jobs)) applyJobs(jobs);
}

async function setOption(key, value) {
  const j = selected();
  if (!j || !S.options || S.options[key] === value) return;
  const res = await call('set_option', j.id, key, value);
  if (!res || S.sel !== j.id) return;
  S.options = res.options;
  if (res.accepted && FAST.has(key) && HAS_RESULT.has(j.status)) { S.changedKey = key; S.showAll = false; }
  else if (res.accepted && HAS_RESULT.has(j.status)) { S.changedKey = null; loadResult(j.id); }  // 달라진 곳 초기화
  render();
  refresh();
}

async function setSaveDir() {
  const dir = await call('set_save_dir');
  if (typeof dir === 'string') { S.saveDir = dir; renderOptions(); }
}

async function primary() {
  const j = selected();
  if (j && HAS_RESULT.has(j.status)) {
    S.changedKey = null;
    await call('save', j.id);
  } else {
    await call('start');
  }
  refresh();
}

// ---- 모델 덮개 -----------------------------------------------------------
const dl = { samples: [] };
const gb = (b) => `${(Math.max(0, b) / 1e9).toFixed(1)}GB`;

function coverProgress(done, total) {
  $('cover').hidden = false;
  $('cover-progress').hidden = false;
  $('cover-error').hidden = true;
  if (!total) { $('cover-bar').style.transform = 'scaleX(0)'; $('cover-text').textContent = ''; return; }
  const now = performance.now() / 1000;
  dl.samples.push([now, done]);
  while (dl.samples.length > 2 && now - dl.samples[0][0] > 20) dl.samples.shift();
  let eta = '';
  const [t0, d0] = dl.samples[0];
  if (now - t0 >= 2 && done > d0 && done < total) {
    const left = (total - done) / ((done - d0) / (now - t0));
    eta = ` · 약 ${Math.max(1, Math.round(left / 60))}분 남음`;
  }
  $('cover-bar').style.transform = `scaleX(${Math.min(1, done / total)})`;
  $('cover-text').textContent = `${gb(done)} / ${gb(total)}${eta}`;
}
function coverError() {
  $('cover').hidden = false;
  $('cover-progress').hidden = true;
  $('cover-error').hidden = false;
}
async function startDownload() {
  dl.samples = [];
  coverProgress(0, 0);
  await call('download_models');
}

// ---- 업데이트 띠 ---------------------------------------------------------
async function checkUpdate() {
  const info = await call('check_update');
  if (!info) return;
  S.update = info;
  $('up-ver').textContent = info.version;
  $('up-body').textContent = info.notes || '';
  $('up-state').textContent = '';
  $('update').hidden = false;
}

// ---- 파이썬 알림 ---------------------------------------------------------
window.onAppEvent = (event, data) => {
  data = data || {};
  if (event === 'job' || event === 'jobs') refresh();
  else if (event === 'models') {
    if (data.error) coverError();
    else if (data.ready) { $('cover').hidden = true; refresh(); }
    else if ('done' in data) coverProgress(data.done, data.total);
  } else if (event === 'update') {
    $('up-state').textContent = data.error ? '받지 못했습니다' : '';
  }
};

// ---- 시작 ----------------------------------------------------------------
function wire() {
  $('primary').addEventListener('click', primary);
  $('pick-big').addEventListener('click', pickFiles);
  $('adv-toggle').addEventListener('click', () => { S.advOpen = !S.advOpen; renderOptions(); });
  $('cover-retry').addEventListener('click', startDownload);
  $('up-notes').addEventListener('click', () => { $('up-body').hidden = !$('up-body').hidden; });
  $('up-get').addEventListener('click', async () => {
    let r = await call('install_update');
    if (r && r.confirm) {
      if (!window.confirm('처리 중인 영상이 있습니다. 멈추고 업데이트할까요?')) return;
      $('up-state').textContent = '받는 중…';
      r = await call('install_update', true);
    } else if (r && r.started) $('up-state').textContent = '받는 중…';
    if (!r || !r.started) $('up-state').textContent = '받지 못했습니다';
  });
  $('up-close').addEventListener('click', () => {
    if (S.update) call('dismiss_update', S.update.version);
    $('update').hidden = true;
  });
  // 파일을 끌어 오는 동안 표시. 실제 추가는 파이썬의 drop 처리기가 한다.
  let depth = 0;
  document.addEventListener('dragenter', (e) => { e.preventDefault(); depth++; document.body.classList.add('dragging'); });
  document.addEventListener('dragover', (e) => { e.preventDefault(); });
  document.addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; document.body.classList.remove('dragging'); } });
  document.addEventListener('drop', (e) => { e.preventDefault(); depth = 0; document.body.classList.remove('dragging'); });
}

let started = false;
async function init() {
  if (started) return;
  started = true;
  const st = await call('model_status');
  if (st && !st.ready) {
    coverProgress(0, 0);
    if (!st.downloading) await call('download_models');
  }
  const settings = await call('get_settings');
  if (settings) S.saveDir = settings.save_dir;
  await refresh();
  render();
  checkUpdate();
  setInterval(checkUpdate, 24 * 3600 * 1000);
}

wire();
render();
if (window.pywebview && window.pywebview.api) init();
else window.addEventListener('pywebviewready', init);
