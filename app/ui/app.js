'use strict';
/* youtube-edit 화면. 파이썬 Api 는 pywebview.api.*, 파이썬 알림은 window.onAppEvent(event, data). */

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

const OPTS = [
  { key: 'filler', label: '망설임 소리', sub: '아·어·음…', choices: [['keep', '유지'], ['remove', '제거']] },
  { key: 'habit', label: '말버릇', sub: '그니까·약간·이제…', choices: [['keep', '유지'], ['some', '일부'], ['all', '모두']] },
  { key: 'silence', label: '말 없는 구간', slider: true, ends: ['안 자름', '최대한 자름'], say: silenceSay },
  { key: 'denoise', label: '배경 잡음 제거', slider: true, ends: ['끔', '최대 (목소리 먹먹해질 수 있음)'], say: denoiseSay },
];
const ADV = [
  { key: 'loudnorm', label: '음량 맞추기', sub: '유튜브 기준 크기(-14 LUFS)로 맞춤', choices: [['keep', '유지'], ['match', '맞춤']] },
  { key: 'subs', label: '영상 안 자막', sub: 'mp4 안에 켜고 끄는 자막 트랙. SRT 파일은 항상 만듦', choices: [['omit', '빼기'], ['embed', '넣기']] },
  { key: 'quality', label: '화질', sub: '자동: 1080p30 8Mbps · 4K30 약 32Mbps', choices: [['low', '작게'], ['auto', '자동'], ['high', '높게']] },
];
const DEFAULTS = { filler: 'remove', habit: 'some', silence: 50, denoise: 50, loudnorm: 'match', subs: 'embed', quality: 'auto' };
const FAST = new Set(['filler', 'habit', 'silence', 'denoise']);

// 슬라이더 설명 한 줄. st: 지금 결과의 말 없는 구간 {n: 곳, sec: 초, min: fillers.json 최소 길이} (결과가 없으면 null)
function silenceSay(v, st) {
  const tail = st && typeof st.n === 'number' ? ` · ${st.n}곳 · −${st.sec}초` : '';
  const min = st && Number.isFinite(st.min) && st.min > 0 ? st.min : 0.3;
  // 파이프라인 silence_level_cfg 와 같은 식: 1~50 은 1.0초 → min (min 이 1초 이상이면 그대로)
  const at = min >= 1.0 ? min : 1.0 - (1.0 - min) * v / 50;
  if (v <= 0) return '말 없는 구간을 자르지 않습니다';
  if (v <= 50) return `조용히 말 없는 구간 중 ${at.toFixed(1)}초 이상만 자름${tail}`;
  if (v < 100) return `조금 시끄러운 말 없는 구간도 자름 (${min.toFixed(1)}초 이상)${tail}`;
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
  // 자막: caps 는 선택한 영상의 자막 줄(원본 초), tab 은 'cuts' | 'subs', editing 은 열린 어절 입력칸 {key}
  tab: 'cuts', renderPending: false, caps: null, capsFor: null, capsVersion: 0, dropped: false, lineSel: null, editing: null,
  findKeys: new Set(), findCount: 0, scroll: {},
  // 타임라인 구간: range 는 {start, end} (원본 초, 한쪽만 정했으면 다른 쪽은 null), rangeBusy 는 요청 중
  range: null, rangeBusy: false, rangeErr: false,
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
  S.caps = null; S.capsFor = null; S.dropped = false; S.lineSel = null; S.editing = null; S.scroll = {};
  S.findKeys = new Set(); S.findCount = 0; findBar.find.value = ''; findBar.repl.value = '';
  S.range = null; S.rangeErr = false; tlDrag = null;
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
  loadCaptions(id);                                 // 다시 판정·되살리기로 잘린 곳이 바뀌면 자막 줄도 바뀐다
  const r = await call('get_result', id);
  if (r && r.edits_dropped) markDropped(id);        // 서버는 한 번만 알려 준다 — 버리는 응답에서도 받아 둔다
  if (seq !== resultSeq || S.sel !== id) return;   // 늦게 온 옛 응답은 버린다
  if (!r) { S.resultKey = null; return; }          // 다음 알림 때 다시 읽는다
  S.result = r; S.resultFor = id; S.resultVersion++;
  if (!r.changes) S.changedKey = null;
  render();
}

let capsSeq = 0;
async function loadCaptions(id) {
  const seq = ++capsSeq;
  const r = await call('get_captions', id);
  if (r && r.edits_dropped) markDropped(id);
  if (seq !== capsSeq || S.sel !== id || !r) return;
  applyCaps(id, r);
}

// 받아쓰기가 바뀌어 고친 자막을 버렸다는 알림 (그 영상을 보고 있을 때만)
function markDropped(id) {
  if (S.sel !== id || S.dropped) return;
  S.dropped = true;
  renderResult();
}

// 자막 API 응답 {captions, edits_dropped} 을 받아 목록·영상 위 자막을 다시 그린다
function applyCaps(id, r) {
  if (S.sel !== id || !r || !Array.isArray(r.captions)) return;
  capsSeq++;                                        // 고친 뒤의 자막이 늦게 온 옛 get_captions 에 덮이지 않게
  S.caps = r.captions; S.capsFor = id; S.capsVersion++;
  if (r.edits_dropped) markDropped(id);
  if (S.lineSel && !S.caps.some((c) => c.key === S.lineSel)) S.lineSel = null;
  renderResult();
  if (findBar.find.value.trim()) findPreview();
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
  if (!s) return null;
  const min = Number(s.silence_min_sec);
  if (typeof s.silence_cuts !== 'number') return Number.isFinite(min) ? { min } : null;
  return { n: s.silence_cuts, sec: Math.round(Number(s.silence_sec) || 0), min };
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
  const key = j ? [j.id, j.status, j.error, S.resultVersion, S.showAll, S.showAllCuts,
    S.capsVersion, S.tab, S.lineSel, S.dropped].join('|') : '';
  // 열린 어절 입력칸·끌고 있는 타임라인을 지우지 않는다 (닫거나 손을 뗄 때 다시 그림)
  if (S.editing || tlDrag) { S.renderPending = true; return; }
  S.renderPending = false;
  if (!force && key === S.drawn) return;
  S.drawn = key;
  if (!j) { box.replaceChildren(); return; }
  const r = S.resultFor === j.id ? S.result : null;
  box.classList.toggle('busy', !!r && BUSY.has(j.status));
  const parts = [];
  if (r && S.dropped) {
    parts.push(h('div', { class: 'notice' }, h('span', {}, '받아쓰기가 바뀌어 고친 자막을 지웠습니다'),
      h('button', { type: 'button', class: 'x', title: '닫기', onclick: () => { S.dropped = false; renderResult(); } }, '✕')));
  }
  if (j.status === 'failed') {
    parts.push(h('div', { class: 'err' }, '실패' + (j.error ? ` — ${j.error}` : '')));
  } else if (j.status === 'review' && j.error) {
    // 저장 실패는 결과를 그대로 두고 이유만 보여 준다 (저장을 바로 다시 누를 수 있다)
    parts.push(h('div', { class: 'err' }, `저장 실패 — ${j.error}`));
  }
  if (r) parts.push(...resultParts(j, r));
  // 재생기는 그대로 두고 나머지만 다시 그린다 (재생 위치·탭별 목록 스크롤 위치·찾기 입력칸 유지)
  const oldList = box.querySelector('.cut-list');
  if (oldList) S.scroll[oldList.dataset.tab] = oldList.scrollTop;
  const ae = document.activeElement;
  const typing = (ae === findBar.find || ae === findBar.repl) ? [ae, ae.selectionStart, ae.selectionEnd] : null;
  box.replaceChildren(...parts);
  const newList = box.querySelector('.cut-list');
  if (newList) newList.scrollTop = S.scroll[newList.dataset.tab] || 0;
  if (typing && typing[0].isConnected) {
    typing[0].focus({ preventScroll: true });
    try { typing[0].setSelectionRange(typing[1], typing[2]); } catch (e) { /* 무시 */ }
  }
  player.subKey = null;
  if (r && r.preview_url) player.attach(r);
  else if (r) player.detach();
  drawRange();
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
  out.push(...(r.preview_url ? [player.slot(), player.sub] : [h('div', { class: 'err' }, '미리보기를 만들지 못했습니다')]));
  out.push(timeline(r), rangeBar.el);
  const back = s.restored || restoreOps(r).length;
  out.push(h('div', { class: 'legend' },
    h('b', { class: 'cut' }, '■ 잘린 곳'), ' · ', h('b', { class: 'changed' }, '■ 달라진 곳'),
    back ? [' · ', h('b', { class: 'back' }, '▭ 되살린 곳')] : []));
  if (r.changes) out.push(...changeRows(r.changes));
  out.push(...listParts(j, r));
  if (j.status === 'saved' && r.saved_paths && r.saved_paths.length) {
    out.push(h('div', { class: 'saved-files' }, r.saved_paths.map((p) => h('div', {}, p))));
  }
  return out;
}

// 타임라인: 그냥 누르면 그 위치로 이동, 4px 이상 끌면 구간 고르기, 구간 양 끝 손잡이로 넓히고 좁히기
function timeline(r) {
  const dur = r.duration || player.duration() || 0;
  const bar = h('div', { class: 'tl' });
  if (dur > 0) {
    const pos = (a, b) => `left:${(a / dur) * 100}%;width:${((b - a) / dur) * 100}%`;
    for (const [a, b] of r.cuts || []) bar.append(h('b', { style: pos(a, b) }));
    for (const c of r.cut_items || []) if (c.restored) bar.append(h('u', { style: pos(c.start, c.end) }));
    for (const o of restoreOps(r)) bar.append(h('u', { style: pos(o.start, o.end) }));
    for (const row of (r.changes && r.changes.rows) || []) bar.append(h('s', { style: pos(row.start, row.end) }));
  }
  bar.append(rangeSel, player.head);
  const at = (x, g) => Math.min(g.d, Math.max(0, ((x - g.rect.left) / g.rect.width) * g.d));
  bar.addEventListener('pointerdown', (e) => {
    const d = rangeDur();
    if (e.button !== 0 || !d) return;
    const rect = bar.getBoundingClientRect();
    let mode = 'press';
    if (e.target.closest('.hd') && S.range && S.range.start !== null && S.range.end !== null) {
      // 짧은 구간은 손잡이가 겹친다 → 누른 곳이 두 손잡이 가운데보다 왼쪽이면 시작, 아니면 끝
      const mid = rect.left + (((S.range.start + S.range.end) / 2) / d) * rect.width;
      mode = e.clientX < mid ? 'start' : 'end';
    }
    tlDrag = { rect, x0: e.clientX, d, mode, anchor: 0 };
    S.rangeErr = false;
    try { bar.setPointerCapture(e.pointerId); } catch (err) { /* 무시 */ }
    e.preventDefault();
  });
  bar.addEventListener('pointermove', (e) => {
    if (!tlDrag) return;
    if (e.buttons === 0) { up(e, false); return; }   // 손 뗀 걸 못 받았다 (창 밖 등)
    if (tlDrag.mode === 'press') {
      if (Math.abs(e.clientX - tlDrag.x0) < 4) return;
      tlDrag.mode = 'new';
      tlDrag.anchor = at(tlDrag.x0, tlDrag);
    }
    const t = at(e.clientX, tlDrag);
    if (tlDrag.mode === 'new') S.range = { start: Math.min(tlDrag.anchor, t), end: Math.max(tlDrag.anchor, t) };
    else setRangeEnd(tlDrag.mode, t, true);
    drawRange();
  });
  const up = (e, cancel) => {
    if (!tlDrag) return;
    const g = tlDrag;
    tlDrag = null;
    if (g.mode === 'new' && S.range) {
      // 0.1초 격자, 영상 안, 최소 0.1초 (손을 놓친 경우에도 맞춰 둔다)
      let a = snapT(S.range.start, g.d), b = snapT(S.range.end, g.d);
      if (b - a < 0.1 - EPS) { b = snapT(ceil10(a + 0.1), g.d); a = Math.max(0, floor10(b - 0.1)); }
      S.range = { start: a, end: b };
    }
    if (cancel) { /* 이동하지 않는다 */ } else if (g.mode === 'press') player.seek(at(e.clientX, g));
    else if (g.mode === 'new') { if (S.range) player.seek(S.range.start); }
    else if (S.range && S.range[g.mode] !== null) player.seek(S.range[g.mode]);
    drawRange();
    flushRender();
  };
  bar.addEventListener('pointerup', (e) => up(e, false));
  bar.addEventListener('pointercancel', (e) => up(e, true));
  bar.addEventListener('lostpointercapture', (e) => up(e, true));   // pointerup 뒤에도 오지만 그땐 tlDrag 가 비어 있다
  return bar;
}

// ---- 구간 직접 자르기·되살리기 -------------------------------------------------
let tlDrag = null;   // 타임라인을 누르고 있는 동안 {rect, x0, d, mode: 'press'|'new'|'start'|'end', anchor}
const EPS = 1e-6;
const q10 = (t) => Math.round(t * 10) / 10;
const floor10 = (t) => Math.floor(t * 10 + EPS) / 10;
const ceil10 = (t) => Math.ceil(t * 10 - EPS) / 10;
// 0.1초 격자에 맞추되 영상 끝을 넘지 않게: 끝 근처(0.05초 안)는 영상 끝 그대로 (API 가 길이를 넘으면 거절한다)
function snapT(t, d) {
  t = q10(Math.min(d, Math.max(0, t)));
  if (t > d || d - t < 0.05) t = d;
  return t < 0 ? 0 : t;
}
const restoreOps = (r) => ((r && r.range_ops) || []).map((o, i) => ({ ...o, i })).filter((o) => o.op === 'restore');
function rangeDur() {
  const r = S.resultFor === S.sel ? S.result : null;
  return (r && r.duration) || player.duration() || 0;
}

// 한쪽 끝을 옮긴다 (0.1초 단위, 영상 안). keep 이면 반대쪽과 0.1초 이상 떨어지게 멈추고,
// 아니면("여기서 시작/끝") 반대쪽을 넘어가면 반대쪽을 비워 새로 고르게 한다
function setRangeEnd(which, t, keep) {
  const d = rangeDur();
  if (!d || t === null || !isFinite(t)) return;
  t = snapT(t, d);
  const R = { ...(S.range || { start: null, end: null }) };
  const other = which === 'start' ? R.end : R.start;
  if (other !== null) {
    const bad = which === 'start' ? t > other - 0.1 + EPS : t < other + 0.1 - EPS;
    if (bad && keep) t = which === 'start' ? Math.max(0, floor10(other - 0.1)) : snapT(ceil10(other + 0.1), d);
    else if (bad) R[which === 'start' ? 'end' : 'start'] = null;
  }
  R[which] = t;
  S.range = R;
  S.rangeErr = false;
}

// 구간 [a, b] 중 지금 잘린 곳에 들어간 길이
function cutOverlap(a, b) {
  const r = S.resultFor === S.sel ? S.result : null;
  let n = 0;
  for (const [x, y] of merge((r && r.cuts) || [])) n += Math.max(0, Math.min(b, y) - Math.max(a, x));
  return n;
}

// 타임라인 위 구간 표시 (한 번 만들어 두고 다시 그릴 때 새 막대에 옮겨 붙인다)
const rangeSel = h('div', { class: 'rsel', hidden: true },
  h('i', { class: 'hd', 'data-end': 'start', title: '시작 옮기기' }), h('i', { class: 'hd', 'data-end': 'end', title: '끝 옮기기' }));

// 타임라인 아래 막대: 버튼은 한 번 만들고 글자·켜짐만 그 자리에서 고친다 (누르는 도중 바뀌어 클릭이 사라지지 않게)
const rangeBar = (() => {
  const btn = (text, cls, fn) => h('button', { type: 'button', class: 'btn' + (cls ? ` ${cls}` : ''), onclick: fn }, text);
  const now = () => (player.video ? player.video.currentTime : null);
  const step = (which, dt) => () => {
    if (!S.range || S.range[which] === null) return;
    setRangeEnd(which, S.range[which] + dt, true);
    if (S.range[which] !== null) player.seek(S.range[which]);
    drawRange();
  };
  const b = {
    from: btn('여기서 시작', null, () => { setRangeEnd('start', now(), false); drawRange(); }),
    to: btn('여기서 끝', null, () => { setRangeEnd('end', now(), false); drawRange(); }),
    a: h('b'), aDown: btn('◀0.1', null, step('start', -0.1)), aUp: btn('0.1▶', null, step('start', 0.1)),
    z: h('b'), zDown: btn('◀0.1', null, step('end', -0.1)), zUp: btn('0.1▶', null, step('end', 0.1)),
    len: h('span', { class: 'muted' }),
    cut: btn('자르기', 'cut', () => rangeOp('cut')),
    back: btn('되살리기', 'on', () => rangeOp('restore')),
    cancel: btn('취소', null, () => { S.range = null; S.rangeErr = false; drawRange(); }),
    hint: h('span', { class: 'muted hint' }, '타임라인을 끌어 구간을 고르세요'),
    err: h('span', { class: 'err' }, '구간을 바꾸지 못했습니다'),
  };
  b.times = h('span', { class: 'rt' }, h('span', { class: 'lt' }, b.a, b.aDown, b.aUp), h('span', { class: 'muted' }, '~'),
    h('span', { class: 'lt' }, b.z, b.zDown, b.zUp), b.len);
  b.acts = h('span', { class: 'lt' }, b.cut, b.back, b.cancel);
  b.el = h('div', { class: 'range-bar' }, h('span', { class: 'lt' }, b.from, b.to), b.hint, b.times, b.acts, b.err);
  return b;
})();

function drawRange() {
  const j = selected();
  const r = j && S.resultFor === j.id ? S.result : null;
  const d = rangeDur();
  const R = S.range;
  const has = !!R && (R.start !== null || R.end !== null);
  // 타임라인 위
  rangeSel.hidden = !has || !d;
  if (has && d) {
    const a = R.start !== null ? R.start : R.end, z = R.end !== null ? R.end : R.start;
    rangeSel.style.left = `${(a / d) * 100}%`;
    rangeSel.style.width = `${((z - a) / d) * 100}%`;
    rangeSel.classList.toggle('mark', R.start === null || R.end === null);
  }
  // 막대 아래
  const b = rangeBar;
  b.el.hidden = !r || !d;
  if (b.el.hidden) return;
  const can = HAS_RESULT.has(j.status) && !BUSY.has(j.status) && !S.rangeBusy;
  b.from.disabled = b.to.disabled = !player.video;
  b.err.hidden = !S.rangeErr;
  b.hint.hidden = has || S.rangeErr;
  b.times.hidden = b.acts.hidden = !has;
  if (!has) return;
  const full = R.start !== null && R.end !== null;
  b.a.textContent = R.start !== null ? clock1(R.start) : '…';
  b.z.textContent = R.end !== null ? clock1(R.end) : '…';
  b.len.textContent = full ? `(${(R.end - R.start).toFixed(1)}초)` : '';
  b.aDown.disabled = b.aUp.disabled = R.start === null;
  b.zDown.disabled = b.zUp.disabled = R.end === null;
  const inCut = full ? cutOverlap(R.start, R.end) : 0;
  b.cut.disabled = !can || !full || R.end - R.start - inCut < 0.05;   // 남아 있는 곳이 있어야 자를 수 있다
  b.back.disabled = !can || !full || inCut < 0.01;                     // 잘린 곳과 겹쳐야 되살릴 수 있다
  b.cancel.disabled = S.rangeBusy;
}

async function rangeOp(kind) {
  const j = selected();
  const R = S.range;
  if (!j || S.rangeBusy || !R || R.start === null || R.end === null) return;
  S.rangeBusy = true;
  S.rangeErr = false;
  drawRange();
  try {
    const res = await call('add_range_op', j.id, kind, R.start, R.end);
    if (S.sel !== j.id) return;
    if (!res) { S.rangeErr = true; return; }   // 거절됨 (영상 밖·상태 등) — 구간은 그대로 둔다
    if (S.range === R) S.range = null;
    await loadResult(j.id);
  } finally {
    S.rangeBusy = false;
    drawRange();
  }
}

// "잘린 곳" 목록의 직접 자름·직접 되살림 줄 지우기 (그 작업 기록을 지움)
async function removeOp(id, index) {
  if (S.rangeBusy) return;
  S.rangeBusy = true;
  S.rangeErr = false;
  drawRange();
  try {
    const res = await call('remove_range_op', id, index);
    if (S.sel !== id) return;
    if (!res) { S.rangeErr = true; return; }
    await loadResult(id);
  } finally {
    S.rangeBusy = false;
    drawRange();
  }
}
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !S.range || S.editing || tlDrag) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
  S.range = null; S.rangeErr = false;
  drawRange();
});

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

const KIND = { silence: '말 없는 구간', filler: '망설임 소리', habit: '말버릇', manual: '직접 자름' };

// 잘린 곳 목록: 하나씩 되살리거나 다시 자를 수 있다 (처리 없이 바로 반영, 저장 때 쓰임).
// 타임라인에서 직접 자른 곳(manual)·직접 되살린 구간은 그 작업 기록을 지워서 되돌린다
function cutRows(j, r) {
  const items = [...(r.cut_items || []), ...restoreOps(r).map((o) => ({ start: o.start, end: o.end, kind: 'restore_op', op: o.i }))]
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const rows = [];
  const shown = S.showAllCuts ? items : items.slice(0, ROWS_FIRST);
  const editable = HAS_RESULT.has(j.status);
  for (const c of shown) {
    if (c.kind === 'manual' || c.kind === 'restore_op') {
      const back = c.kind === 'restore_op';
      rows.push(h('div', { class: 'opt row' + (back ? ' op-back' : ''), onclick: () => player.clip(c.start, c.end, true) },
        h('span', {}, h('span', { class: 'w' }, `${clock(c.start)} ${back ? '직접 되살림' : '직접 자름'}`),
          h('span', { class: 'sec' }, `${(c.end - c.start).toFixed(1)}초`)),
        h('button', { type: 'button', class: 'btn' + (back ? ' on' : ''), disabled: !editable,
          onclick: (e) => { e.stopPropagation(); removeOp(j.id, c.op); } }, back ? '다시 자르기' : '되살리기')));
      continue;
    }
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
  return rows;
}

// "잘린 곳" 자리를 탭 두 개로 나눠 쓴다. 목록만 따로 스크롤한다 (재생기·타임라인은 그대로 보이게)
function listParts(j, r) {
  const items = r.cut_items || [];
  const n = items.filter((c) => c.restored).length + restoreOps(r).length;
  const caps = S.capsFor === j.id ? S.caps : null;
  if (!items.length && !n && !(caps && caps.length)) return [];
  const tab = (key, text) => h('button', { type: 'button', class: S.tab === key ? 'on' : null,
    onclick: () => { if (S.tab !== key) { S.tab = key; renderResult(); } } }, text);
  const out = [h('div', { class: 'tabs', role: 'tablist' },
    tab('cuts', `잘린 곳 ${items.length}` + (n ? ` · 되살림 ${n}` : '')),
    tab('subs', caps ? `자막 ${caps.filter((c) => !c.empty).length}줄` : '자막'))];
  if (S.tab === 'subs') {
    syncFindBar(HAS_RESULT.has(j.status));
    out.push(findBar.el, h('div', { class: 'cut-list subs', 'data-tab': 'subs' }, (caps || []).map((c) => capRow(j, c))));
  } else {
    out.push(h('div', { class: 'cut-list', 'data-tab': 'cuts' }, cutRows(j, r)));
  }
  return out;
}

// ---- 자막 고치기 -----------------------------------------------------------
// m:ss.s (줄 시간 편집기)
function clock1(sec) {
  const d = Math.max(0, Math.round((Number(sec) || 0) * 10));
  return `${Math.floor(d / 600)}:${((d % 600) / 10).toFixed(1).padStart(4, '0')}`;
}

// 자막 탭의 한 줄: 시간(누르면 그 줄 들어보기 + 줄 시간 편집기) + 어절들
function capRow(j, c) {
  const sel = S.lineSel === c.key;
  return h('div', { class: 'cap' + (sel ? ' sel' : '') + (c.timed ? ' timed' : '') + (c.empty ? ' empty' : '') },
    h('button', { type: 'button', class: 't', title: c.timed ? '시간을 고친 줄' : null, onclick: () => {
      player.clip(c.start, c.end, true);
      if (S.lineSel !== c.key) { S.lineSel = c.key; renderResult(); }
    } }, clock(c.start)),
    h('span', { class: 'ws' }, c.words.map((w) => wordEl(w, 'list'))),
    sel && !c.empty ? lineEditor(j, c) : null);
}

// 어절 하나. 자막 탭과 영상 위 자막이 같이 쓴다 (영상 위는 멈췄을 때만 고침).
// 빈칸으로 뺀 어절(text "")은 원래 글자에 줄을 그어 남긴다 (눌러서 원래대로 되돌릴 수 있게)
function wordEl(w, where) {
  const j = selected();
  const can = !!j && HAS_RESULT.has(j.status);
  const gone = !w.text;
  const el = h('span', { class: 'w' + (can ? ' can' : '') + (w.edited ? ' edited' : '') + (gone ? ' gone' : '') +
    (S.findKeys.has(w.key) ? ' hit' : ''),
    'data-k': w.key, title: gone ? '자막에서 뺀 어절' : w.edited ? `원래: ${w.orig}` : null }, gone ? w.orig : w.text);
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    if (where === 'over' && player.video && !player.video.paused) return;   // 재생 중엔 보기만
    editWord(el, w, where);
  });
  return el;
}

// 누르고 있는 동안 그 아래 버튼을 바꾸면 클릭이 사라진다 → 손을 뗀 뒤(클릭까지 끝난 뒤)에 고친다
let pointerDown = false;
document.addEventListener('pointerdown', () => { pointerDown = true; }, true);
for (const ev of ['pointerup', 'pointercancel']) document.addEventListener(ev, () => { pointerDown = false; }, true);
function afterPointer(fn) {
  if (!pointerDown) { fn(); return; }
  let fired = false;
  const go = () => {
    if (fired) return;
    fired = true;
    document.removeEventListener('pointerup', go, true);
    document.removeEventListener('pointercancel', go, true);
    setTimeout(fn, 0);
  };
  document.addEventListener('pointerup', go, true);
  document.addEventListener('pointercancel', go, true);
  setTimeout(go, 2000);   // 창 밖에서 손을 떼 pointerup 을 못 받은 경우
}
// 입력칸이 열려 있어 미뤄 둔 다시 그리기
function flushRender() {
  if (S.renderPending && !S.editing) renderResult(true);
}

// 그 자리를 입력칸으로: Enter·바깥 클릭 확정, Esc 취소, 빈칸 확정 = 그 어절 빼기
function editWord(el, w, where) {
  const j = selected();
  if (!j || !HAS_RESULT.has(j.status) || S.editing) return;
  const id = j.id;
  S.editing = { key: w.key };
  const input = h('input', { type: 'text', class: 'w-in', value: w.text, spellcheck: 'false', 'aria-label': '어절 고치기' });
  const fit = () => { input.style.width = `${Math.max(2, input.value.length) + 1.5}em`; };
  fit();
  let done = false;
  let wrap = null;
  // 닫으면 바로 다른 어절을 열 수 있다 (S.editing 을 먼저 비움). 그 자리는 고친 글자로 바로 바꿔 두고,
  // 서버 응답(전체 다시 그리기)은 손을 뗀 뒤에 반영한다
  const finish = (mode) => {
    if (done) return;
    done = true;
    S.editing = null;
    const text = mode === 'orig' ? null : input.value;
    const change = mode !== 'cancel' && (text === null || text.trim() !== w.text);
    const shown = !change ? w : text === null ? { ...w, text: w.orig, edited: false } : { ...w, text: text.trim(), edited: true };
    input.readOnly = true;
    afterPointer(() => {
      if (wrap.isConnected) wrap.replaceWith(wordEl(shown, where));
      flushRender();
    });
    if (!change) return;
    call('set_word', id, w.key, text).then((r) => afterPointer(() => {
      if (r) applyCaps(id, r);
      else if (S.sel === id) renderResult(true);   // 거절됨 → 서버 상태대로 다시 그림
    }));
  };
  input.addEventListener('input', fit);
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); finish('ok'); }
    else if (e.key === 'Escape') { e.preventDefault(); finish('cancel'); }
  });
  input.addEventListener('blur', () => finish('ok'));
  input.addEventListener('click', (e) => e.stopPropagation());
  const back = w.edited ? h('button', { type: 'button', class: 'btn', onmousedown: (e) => e.preventDefault(),
    onclick: (e) => { e.stopPropagation(); finish('orig'); } }, '원래대로') : null;
  wrap = h('span', { class: 'w-edit' }, input, back);
  el.replaceWith(wrap);
  input.focus();
  input.select();
}

// 줄 시간 편집기: 시작·끝을 0.1초씩 또는 재생기 지금 위치로 (서버가 이웃 줄·영상 길이·최소 0.3초로 맞춘다)
function lineEditor(j, c) {
  const can = HAS_RESULT.has(j.status);
  const set = (s, e) => setLineTime(j.id, c.key, s, e);
  const now = () => (player.video ? player.video.currentTime : null);
  const part = (label, t, which) => {
    const go = (v) => (v === null ? null : which === 'start' ? set(v, null) : set(null, v));
    return h('span', { class: 'lt' }, `${label} `, h('b', {}, clock1(t)),
      h('button', { type: 'button', class: 'btn', disabled: !can, onclick: () => go(t - 0.1) }, '◀0.1'),
      h('button', { type: 'button', class: 'btn', disabled: !can, onclick: () => go(t + 0.1) }, '0.1▶'),
      h('button', { type: 'button', class: 'btn', disabled: !can || !player.video, onclick: () => go(now()) }, '지금 위치로'));
  };
  return h('div', { class: 'line-time' }, part('시작', c.start, 'start'), part('끝', c.end, 'end'),
    h('button', { type: 'button', class: 'btn', disabled: !can || !c.timed, onclick: () => set(null, null) }, '원래대로'));
}

async function setLineTime(id, key, start, end) {
  const r = await call('set_line_time', id, key, start, end);
  if (r) applyCaps(id, r);
}

// 찾기 막대: 한 번 만들어 두고 다시 그릴 때 옮겨 붙인다 (입력 중인 글자·커서 유지)
const findBar = (() => {
  const find = h('input', { type: 'text', spellcheck: 'false', 'aria-label': '찾을 말' });
  const repl = h('input', { type: 'text', spellcheck: 'false', 'aria-label': '바꿀 말' });
  const n = h('span', { class: 'n' });
  const btn = h('button', { type: 'button', class: 'btn' }, '전부 바꾸기');
  const el = h('div', { class: 'find' }, h('span', {}, '찾기'), find, h('span', { class: 'muted' }, '→'), repl, btn, n);
  return { find, repl, n, btn, el };
})();
let findTimer = 0, findSeq = 0;
function syncFindBar(can) {
  findBar.find.disabled = !can; findBar.repl.disabled = !can;
  findBar.btn.disabled = !can || S.findCount <= 0 || applying;
  findBar.n.textContent = `· ${S.findCount}곳`;
}
async function findPreview() {
  const seq = ++findSeq;
  const id = S.sel, q = findBar.find.value, rp = findBar.repl.value;
  let keys = [], count = 0;
  if (id && q.trim()) {
    const r = await call('replace_text', id, q, rp, false);
    if (r && r.edits_dropped) markDropped(id);
    if (seq !== findSeq || S.sel !== id) return;
    if (r) { keys = r.keys || []; count = r.count || 0; }
  }
  S.findKeys = new Set(keys); S.findCount = count;
  paintHits();
}
// 찾은 어절 강조·곳 수만 그 자리에서 고친다 (입력할 때마다 목록을 다시 만들지 않는다)
function paintHits() {
  const j = selected();
  syncFindBar(!!j && HAS_RESULT.has(j.status));
  for (const el of $('result').querySelectorAll('.w[data-k]')) el.classList.toggle('hit', S.findKeys.has(el.dataset.k));
}
// 버튼은 곳 수가 0이면 꺼져 있다. Enter 는 미리보기 대기 중에도 바로 바꾼다 (빈칸이면 서버가 아무것도 안 함)
let applying = false;
async function applyReplace() {
  const j = selected();
  const q = findBar.find.value;
  if (applying || !j || !HAS_RESULT.has(j.status) || !q.trim()) return;
  applying = true;
  findBar.btn.disabled = true;
  clearTimeout(findTimer);
  findSeq++;
  try {
    const r = await call('replace_text', j.id, q, findBar.repl.value, true);
    if (r && r.edits_dropped) markDropped(j.id);
    if (r && r.captions) applyCaps(j.id, { captions: r.captions, edits_dropped: r.edits_dropped });
    else findPreview();
  } finally {
    applying = false;
    paintHits();
  }
}
for (const inp of [findBar.find, findBar.repl]) {
  inp.addEventListener('input', () => { clearTimeout(findTimer); findTimer = setTimeout(findPreview, 150); });
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); applyReplace(); }
  });
}
findBar.btn.addEventListener('click', applyReplace);

async function setRestored(id, c, on) {
  const r = await call('set_restored', id, c.start, c.end, on);
  if (r) loadResult(id);
}

// ---- 미리보기 재생기: 잘린 구간을 건너뛰며 재생 ---------------------------
const player = {
  video: null, url: null, ranges: [], win: null, raf: 0, ownSeek: false,
  head: h('i', { class: 'head', style: 'left:0' }),
  sub: h('div', { class: 'subline empty' }), subKey: null,

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
    this.drawSub(t);
  },
  // 영상 아래 자막: 지금 시간의 줄 (고친 줄 시간 반영). 줄이 바뀔 때만 다시 만든다
  drawSub(t) {
    if (S.editing) return;
    const caps = (S.capsFor === S.sel && S.caps) || [];
    const line = this.video ? caps.find((c) => !c.empty && c.start <= t && t < c.end) : null;
    const key = line ? `${line.key}|${S.capsVersion}` : '';
    if (key === this.subKey) return;
    this.subKey = key;
    this.sub.classList.toggle('empty', !line);
    this.sub.replaceChildren(...(line ? line.words.map((w) => wordEl(w, 'over')) : []));
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
