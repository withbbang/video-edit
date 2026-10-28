'use strict';
/* youtube-edit 화면. 파이썬 Api 는 pywebview.api.*, 파이썬 알림은 window.onAppEvent(event, data). */

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

const OPTS = [
  { key: 'filler', label: '망설임', sub: '아·어·음…', choices: [['keep', '유지'], ['remove', '제거']] },
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

const S = {
  jobs: [], sel: null, options: null,
  result: null, resultFor: null, resultKey: null,
  saveDir: '', advOpen: false, update: null,
  resultVersion: 0, drawn: null,
  // 자막: caps 는 선택한 영상의 자막 줄(원본 초), tab 은 'cuts' | 'subs', editing 은 열린 어절 입력칸 {key}
  // cutKind 는 잘린 곳 목록의 종류 탭 'all' | 'silence' | 'filler' | 'habit'
  tab: 'cuts', cutKind: 'all', renderPending: false, caps: null, capsFor: null, capsVersion: 0, dropped: false, lineSel: null, editing: null,
  findKeys: new Set(), findCount: 0, scroll: {},
  // 타임라인 구간: range 는 {start, end} (원본 초), rangeBusy 는 요청 중
  // mark 는 Shift+클릭·`여기서 시작` 으로 찍어 둔 시작점(끝을 기다림), rangeEnd 는 ←/→ 로 옮길 끝 'start' | 'end'
  range: null, rangeBusy: false, rangeErr: false, mark: null, rangeEnd: 'end',
  // 확대 타임라인: view 는 {t0, spc} (timeline.js), thumbs 는 get_thumbs 결과(없으면 false), wave 는 {hop, bytes}(없으면 false)
  view: null, thumbs: null, wave: null, tlDataFor: null,
  // 편집 되돌리기: hist 는 서버가 알려 준 {undo, redo} 가능 여부, histBusy 는 요청 중
  hist: { undo: false, redo: false }, histBusy: false,
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
  S.caps = null; S.capsFor = null; S.dropped = false; S.lineSel = null; S.editing = null; S.scroll = {};
  S.findKeys = new Set(); S.findCount = 0; findBar.find.value = ''; findBar.repl.value = '';
  S.range = null; S.rangeErr = false; S.mark = null; S.rangeEnd = 'end'; tlDrag = null;
  S.view = null; S.thumbs = null; S.wave = null; S.tlDataFor = null; zoomTl.reset();
  S.hist ={ undo: false, redo: false };
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
  noteHist(id, r);
  render();
  if (S.tlDataFor !== id) { S.tlDataFor = id; loadThumbs(id); loadWave(id); }
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
  noteHist(id, r);
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

function segRow(def, value, disabled, onPick) {
  return h('div', { class: 'opt' + (disabled ? ' disabled' : '') },
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
function sliderRow(def, value, disabled) {
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
  return h('div', { class: 'opt slider' + (disabled ? ' disabled' : '') },
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
  const key = JSON.stringify([j && j.id, j && j.name, o, disabled, S.advOpen, S.saveDir]);
  if (!force && key === optionsDrawn) { for (const show of sliderShows) show(); return; }
  optionsDrawn = key;
  $('opt-title').textContent = j ? `${j.name} 옵션` : '';
  sliderShows = [];
  $('options').replaceChildren(...OPTS.map((d) => (d.slider ? sliderRow : segRow)(d, o[d.key], disabled, setOption)));
  $('adv-arrow').textContent = S.advOpen ? '▾' : '▸';
  $('adv').hidden = !S.advOpen;
  $('adv').replaceChildren(
    ...ADV.map((d) => segRow(d, o[d.key], disabled, setOption)),
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
  const key = j ? [j.id, j.status, j.error, S.resultVersion,
    S.capsVersion, S.tab, S.cutKind, S.lineSel, S.dropped].join('|') : '';
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
  if (r && (r.preview || r.preview_url)) player.attach(r);
  else if (r) player.detach();
  drawRange();
  drawHist();
}

function resultParts(j, r) {
  const s = r.summary || {};
  const out = [];
  out.push(h('div', { class: 'summary' },
    h('span', {}, h('b', { class: 'stat' }, `${clock(s.orig_sec)} → ${clock(s.final_sec)}`), ' · ',
      h('span', { class: 'stat' }, `${s.cuts || 0}곳 잘림`), ' · ',
      h('span', { class: 'stat' }, `−${Math.round(Number(s.removed_sec) || 0)}초`),
      s.restored ? [' · ', h('span', { class: 'stat back' }, `${s.restored}곳 되살림`)] : [])));
  // 왼쪽: 재생기·타임라인, 오른쪽: 잘린 곳·자막 목록 (탭과 함께)
  const main = [];
  // 미리보기를 못 만들어도 컷·요약은 그대로 쓸 수 있다
  main.push(...(r.preview || r.preview_url ? [player.slot(), player.sub] : [h('div', { class: 'err' }, '미리보기를 만들지 못했습니다')]));
  main.push(timeline(r), zoomTl.el, rangeBar.el);
  const back = s.restored || restoreOps(r).length;
  main.push(h('div', { class: 'legend' },
    h('span', {}, h('b', { class: 'cut' }, '■ 잘린 곳'), back ? [' · ', h('b', { class: 'back' }, '▭ 되살린 곳')] : []),
    histBar.el));
  if (j.status === 'saved' && r.saved_paths && r.saved_paths.length) {
    main.push(h('div', { class: 'saved-files' }, r.saved_paths.map((p) => h('div', {}, p))));
  }
  const side = listParts(j, r);
  out.push(h('div', { class: 'res-cols' }, h('div', { class: 'res-main' }, main),
    side.length ? h('div', { class: 'res-side' }, side) : null));
  return out;
}

// 전체 막대: 영상 전체를 한 줄에. 조작은 rangePointer (아래 화면 줄·소리 줄과 같다).
// 노란 틀(viewWin)은 아래 줄들이 지금 보여 주는 범위
function timeline(r) {
  const dur = r.duration || player.duration() || 0;
  const bar = h('div', { class: 'tl' });
  if (dur > 0) {
    const pos = (a, b) => `left:${(a / dur) * 100}%;width:${((b - a) / dur) * 100}%`;
    for (const [a, b] of r.cuts || []) bar.append(h('b', { style: pos(a, b) }));
    for (const c of r.cut_items || []) if (c.restored) bar.append(h('u', { style: pos(c.start, c.end) }));
    for (const o of restoreOps(r)) bar.append(h('u', { style: pos(o.start, o.end) }));
  }
  bar.append(rangeSel, rangeMark, viewWin, player.head);
  rangePointer(bar, (x, rect, d) => ((x - rect.left) / rect.width) * d, (e, rect, d) => {
    if (!e.target.closest('.hd') || !S.range) return null;
    // 짧은 구간은 손잡이가 겹친다 → 누른 곳이 두 손잡이 가운데보다 왼쪽이면 시작, 아니면 끝
    const mid = rect.left + (((S.range.start + S.range.end) / 2) / d) * rect.width;
    return e.clientX < mid ? 'start' : 'end';
  });
  return bar;
}

// 타임라인 누르기·끌기 (전체 막대·화면 줄·소리 줄이 함께 쓴다): 그냥 누르면 그 위치로 이동, 4px 이상 끌면 구간 고르기,
// 구간 양 끝 손잡이로 넓히고 좁히기, Shift+클릭은 홀수 번째가 시작·짝수 번째가 끝.
// toT(clientX, rect, d) → 원본 초, endAt(e, rect, d) → 누른 곳이 구간 손잡이면 'start' | 'end'
function rangePointer(el, toT, endAt) {
  const at = (x, g) => Math.min(g.d, Math.max(0, toT(x, g.rect, g.d)));
  el.addEventListener('pointerdown', (e) => {
    const d = rangeDur();
    if (e.button !== 0 || !d) return;
    const rect = el.getBoundingClientRect();
    const end = endAt(e, rect, d);
    if (end) S.rangeEnd = end;
    tlDrag = { rect, x0: e.clientX, d, mode: end || 'press', anchor: 0, shift: e.shiftKey };
    S.rangeErr = false;
    try { el.setPointerCapture(e.pointerId); } catch (err) { /* 무시 */ }
    e.preventDefault();
  });
  el.addEventListener('pointermove', (e) => {
    if (!tlDrag) return;
    if (e.buttons === 0) { up(e, false); return; }   // 손 뗀 걸 못 받았다 (창 밖 등)
    if (tlDrag.mode === 'press') {
      if (Math.abs(e.clientX - tlDrag.x0) < 4) return;
      tlDrag.mode = 'new';
      tlDrag.anchor = at(tlDrag.x0, tlDrag);
      S.mark = null;
    }
    const t = at(e.clientX, tlDrag);
    if (tlDrag.mode === 'new') S.range = { start: Math.min(tlDrag.anchor, t), end: Math.max(tlDrag.anchor, t) };
    else setRangeEnd(tlDrag.mode, t);
    drawRange();
  });
  const up = (e, cancel) => {
    if (!tlDrag) return;
    const g = tlDrag;
    tlDrag = null;
    if (g.mode === 'new' && S.range) {
      // 프레임 격자, 영상 안, 최소 MIN_LEN (손을 놓친 경우에도 맞춰 둔다). ←/→ 는 끌던 쪽 끝을 옮긴다
      S.rangeEnd = S.range.start < g.anchor - EPS ? 'start' : 'end';
      S.range = makeRange(S.range.start, S.range.end, g.d);
    }
    let t = null;
    if (cancel) { /* 이동하지 않는다 */ } else if (g.mode === 'press' && g.shift) shiftClick(at(e.clientX, g));
    else if (g.mode === 'press') t = at(e.clientX, g);
    else if (g.mode === 'new') t = S.range ? S.range.start : null;
    else if (S.range) t = S.range[g.mode];
    if (t !== null) { player.seek(t); ensureVisible(t); }
    drawRange();
    flushRender();
  };
  el.addEventListener('pointerup', (e) => up(e, false));
  el.addEventListener('pointercancel', (e) => up(e, true));
  el.addEventListener('lostpointercapture', (e) => up(e, true));   // pointerup 뒤에도 오지만 그땐 tlDrag 가 비어 있다
}

// ---- 편집 되돌리기·다시 실행 ------------------------------------------------
// 잘린 곳 되살리기·구간 작업·자막 고치기를 한 단계씩 (옵션 변경은 다시 판정이라 넣지 않는다)
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const histBar = (() => {
  const btn = (text, title, forward) => h('button', { type: 'button', class: 'btn', title, onclick: () => histStep(forward) }, text);
  const undo = btn('↶ 실행 취소', MAC ? '⌘Z' : 'Ctrl+Z', false);
  const redo = btn('↷ 다시 실행', MAC ? '⇧⌘Z' : 'Ctrl+Y', true);
  return { undo, redo, el: h('span', { class: 'hist' }, undo, redo) };
})();

function noteHist(id, r) {
  if (S.sel !== id || !r || !r.history) return;
  S.hist = r.history;
  drawHist();
}

function drawHist() {
  const j = selected();
  const can = !!j && HAS_RESULT.has(j.status) && !S.histBusy;
  histBar.undo.disabled = !can || !S.hist.undo;
  histBar.redo.disabled = !can || !S.hist.redo;
}

async function histStep(forward) {
  const j = selected();
  if (!j || S.histBusy || !HAS_RESULT.has(j.status) || !(forward ? S.hist.redo : S.hist.undo)) return;
  S.histBusy = true;
  drawHist();
  try {
    const res = await call(forward ? 'redo' : 'undo', j.id);
    if (S.sel !== j.id) return;
    if (res) S.hist = res.history;
    await loadResult(j.id);   // 잘린 곳·자막 모두 다시 읽는다
  } finally {
    S.histBusy = false;
    drawHist();
  }
}

document.addEventListener('keydown', (e) => {
  if (!(MAC ? e.metaKey : e.ctrlKey) || e.altKey) return;
  const k = e.key.toLowerCase();
  const forward = k === 'y' ? true : k === 'z' ? e.shiftKey : null;
  if (forward === null) return;
  const t = e.target;
  if (S.editing || (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'))) return;   // 입력칸은 글자 되돌리기
  e.preventDefault();
  histStep(forward);
});

// ---- 구간 직접 자르기·되살리기 -------------------------------------------------
let tlDrag = null;   // 타임라인을 누르고 있는 동안 {rect, x0, d, mode: 'press'|'new'|'start'|'end', anchor, shift}
const EPS = 1e-6;
const MIN_LEN = 0.05;   // 서버가 받는 가장 짧은 구간
// 구간 끝은 원본 프레임 격자에 맞춘다 (렌더도 프레임 단위로 자른다). fps 를 모르면 30
function fps() {
  const r = S.resultFor === S.sel ? S.result : null;
  const f = Number(r && r.fps);
  return f > 0 && isFinite(f) ? f : 30;
}
const fFloor = (t) => Math.floor(t * fps() + EPS) / fps();
const fCeil = (t) => Math.ceil(t * fps() - EPS) / fps();
const minGap = () => (Math.floor(MIN_LEN * fps()) + 1) / fps();   // MIN_LEN 을 넘는 가장 적은 프레임 수
// 프레임 격자에 맞추되 영상 끝을 넘지 않게: 끝 근처(반 프레임 안)는 영상 끝 그대로 (API 가 길이를 넘으면 거절한다)
function snapT(t, d) {
  const f = fps();
  t = Math.round(Math.min(d, Math.max(0, t)) * f) / f;
  if (t > d || d - t < 0.5 / f) t = d;
  return t < 0 ? 0 : t;
}
// 두 시간(순서 무관)을 프레임 격자의 구간으로. 짧으면 minGap 까지 넓힌다
function makeRange(x, y, d) {
  let a = snapT(Math.min(x, y), d), b = snapT(Math.max(x, y), d);
  const g = minGap();
  if (b - a < g - EPS) { b = snapT(fCeil(a + g), d); a = Math.max(0, fFloor(b - g)); }
  return { start: a, end: b };
}
const restoreOps = (r) => ((r && r.range_ops) || []).map((o, i) => ({ ...o, i })).filter((o) => o.op === 'restore');
function rangeDur() {
  const r = S.resultFor === S.sel ? S.result : null;
  return (r && r.duration) || player.duration() || 0;
}

// 한쪽 끝을 옮긴다 (프레임 단위, 영상 안). 반대쪽과 MIN_LEN 이상 떨어진 곳에서 멈춘다
function setRangeEnd(which, t) {
  const d = rangeDur();
  if (!d || !S.range || t === null || !isFinite(t)) return;
  t = snapT(t, d);
  if (which === 'start') t = Math.min(t, Math.max(0, fFloor(S.range.end - minGap())));
  else t = Math.max(t, snapT(fCeil(S.range.start + minGap()), d));
  S.range = { ...S.range, [which]: t };
  S.rangeEnd = which;
  S.rangeErr = false;
}

// 한쪽 끝을 n 프레임 옮기고 그 프레임을 보여 준다 (◀▶ 버튼·←/→)
function stepEnd(which, n) {
  if (!S.range) return;
  setRangeEnd(which, S.range[which] + n / fps());
  player.seek(S.range[which]);
  ensureVisible(S.range[which]);
  drawRange();
}

// 시간 t 를 구간 시작·끝으로 (`여기서 시작`·`여기서 끝`, Shift+클릭 두 번째).
// 시작: 지금 구간의 끝보다 앞이면 시작만 옮기고, 아니면 구간을 버리고 시작점만 찍어 둔다.
// 끝: 찍어 둔 시작점이 있으면 그 둘로, 지금 구간의 시작보다 뒤면 끝만 옮기고, 아니면 영상 처음부터.
function pickEnd(which, t) {
  const d = rangeDur();
  if (!d || t === null || !isFinite(t)) return;
  t = snapT(t, d);
  const R = S.range;
  S.rangeErr = false;
  if (which === 'start') {
    if (R && t <= R.end - minGap() + EPS) setRangeEnd('start', t);
    else { S.range = null; S.mark = t; }
  } else if (S.mark !== null) {
    S.range = makeRange(S.mark, t, d);
    S.rangeEnd = t < S.mark ? 'start' : 'end';
    S.mark = null;
  } else if (R && t >= R.start + minGap() - EPS) setRangeEnd('end', t);
  else { S.range = makeRange(0, t, d); S.rangeEnd = 'end'; }
  player.seek(t);
  ensureVisible(t);
  drawRange();
}

// Shift+클릭: 찍어 둔 시작점이 없으면 새 시작점(지금 구간은 버림), 있으면 끝
function shiftClick(t) {
  const d = rangeDur();
  if (!d) return;
  if (S.mark === null) {
    S.range = null;
    S.mark = snapT(t, d);
    S.rangeErr = false;
    player.seek(S.mark);
    ensureVisible(S.mark);
    drawRange();
  } else pickEnd('end', t);
}

const typingIn = (t) => !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);

// m:ss.ss (구간 시작·끝)
function clock2(sec) {
  const c = Math.max(0, Math.round((Number(sec) || 0) * 100));
  return `${Math.floor(c / 6000)}:${((c % 6000) / 100).toFixed(2).padStart(5, '0')}`;
}
// 직접 입력한 시간: "1:02.35", "62.35", "1:01:02.3" → 초 (못 읽으면 null)
function parseClock(text) {
  const parts = String(text).trim().replace(',', '.').split(':');
  if (parts.length > 3 || parts.some((p, i) => !(i === parts.length - 1 ? /^\d+(\.\d*)?$/ : /^\d+$/).test(p))) return null;
  return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
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
// 찍어 둔 시작점 (끝을 기다리는 동안 점선)
const rangeMark = h('i', { class: 'rmark', hidden: true });

// 타임라인 아래 막대: 버튼은 한 번 만들고 글자·켜짐만 그 자리에서 고친다 (누르는 도중 바뀌어 클릭이 사라지지 않게)
const rangeBar = (() => {
  const btn = (text, cls, fn) => h('button', { type: 'button', class: 'btn' + (cls ? ` ${cls}` : ''), onclick: fn }, text);
  const step = (which, n) => () => stepEnd(which, n);
  const here = (which) => () => { if (player.ready()) pickEnd(which, player.time()); };
  // 시간 칸: 눌러서 직접 입력. Enter·칸 밖을 누르면 반영, Esc 는 취소
  const timeIn = (which, label) => {
    const el = h('input', { type: 'text', class: 'tin', spellcheck: 'false', 'aria-label': label, title: '눌러서 직접 입력 (분:초.00)' });
    let cancel = false;
    el.addEventListener('focus', () => setTimeout(() => el.select(), 0));
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel = true; el.blur(); }
    });
    el.addEventListener('blur', () => {
      const t = cancel ? null : parseClock(el.value);
      cancel = false;
      if (t !== null && S.range) { setRangeEnd(which, t); player.seek(S.range[which]); }   // ←/→ 도 이 끝을 옮긴다
      drawRange();
    });
    return el;
  };
  const frameBtn = (which, n) => {
    const el = btn(n < 0 ? '◀' : '▶', null, step(which, n));
    el.title = n < 0 ? '1프레임 앞으로 (←)' : '1프레임 뒤로 (→)';
    return el;
  };
  const hereBtn = (which, text, title) => {
    const el = btn(text, null, here(which));
    el.title = title;
    return el;
  };
  const b = {
    setA: hereBtn('start', '여기서 시작', '지금 위치를 구간 시작으로 (I)'),
    setZ: hereBtn('end', '여기서 끝', '지금 위치를 구간 끝으로 (O)'),
    a: timeIn('start', '구간 시작'), aDown: frameBtn('start', -1), aUp: frameBtn('start', 1),
    z: timeIn('end', '구간 끝'), zDown: frameBtn('end', -1), zUp: frameBtn('end', 1),
    len: h('span', { class: 'muted' }),
    cut: btn('자르기', 'cut', () => rangeOp('cut')),
    back: btn('되살리기', 'on', () => rangeOp('restore')),
    cancel: btn('취소', null, () => { S.range = null; S.mark = null; S.rangeErr = false; drawRange(); }),
    hint: h('span', { class: 'muted hint' }),
    err: h('span', { class: 'err' }, '구간을 바꾸지 못했습니다'),
  };
  b.times = h('span', { class: 'rt' }, h('span', { class: 'lt' }, b.a, b.aDown, b.aUp), h('span', { class: 'muted' }, '~'),
    h('span', { class: 'lt' }, b.z, b.zDown, b.zUp), b.len);
  b.acts = h('span', { class: 'lt' }, b.cut, b.back, b.cancel);
  b.el = h('div', { class: 'range-bar' }, h('span', { class: 'lt' }, b.setA, b.setZ), b.hint, b.times, b.acts, b.err);
  return b;
})();

function drawRange() {
  const j = selected();
  const r = j && S.resultFor === j.id ? S.result : null;
  const d = rangeDur();
  const R = S.range;
  // 타임라인 위
  rangeSel.hidden = !R || !d;
  if (R && d) {
    rangeSel.style.left = `${(R.start / d) * 100}%`;
    rangeSel.style.width = `${((R.end - R.start) / d) * 100}%`;
    for (const hd of rangeSel.children) hd.classList.toggle('on', hd.dataset.end === S.rangeEnd);
  }
  rangeMark.hidden = S.mark === null || !d;
  if (!rangeMark.hidden) rangeMark.style.left = `${(S.mark / d) * 100}%`;
  zoomTl.draw();
  // 막대 아래
  const b = rangeBar;
  b.el.hidden = !r || !d;
  if (b.el.hidden) return;
  const can = HAS_RESULT.has(j.status) && !BUSY.has(j.status) && !S.rangeBusy;
  b.err.hidden = !S.rangeErr;
  b.hint.hidden = !!R || S.rangeErr;
  b.hint.textContent = S.mark !== null ? '끝 위치를 Shift+클릭하거나 ‘여기서 끝’을 누르세요 (Esc 취소)'
    : '타임라인을 끌거나 Shift+클릭 두 번으로 구간을 선택하세요';
  b.setA.disabled = b.setZ.disabled = S.rangeBusy;
  b.times.hidden = b.acts.hidden = !R;
  if (!R) return;
  for (const [el, t] of [[b.a, R.start], [b.z, R.end]]) {
    if (document.activeElement !== el) el.value = clock2(t);   // 입력 중인 글자는 그대로
  }
  b.len.textContent = `(${(R.end - R.start).toFixed(2)}초 · ${Math.round((R.end - R.start) * fps())}프레임)`;
  const inCut = cutOverlap(R.start, R.end);
  b.cut.disabled = !can || R.end - R.start - inCut < 0.05;   // 남아 있는 곳이 있어야 자를 수 있다
  b.back.disabled = !can || inCut < 0.01;                     // 잘린 곳과 겹쳐야 되살릴 수 있다
  b.cancel.disabled = S.rangeBusy;
}

// ---- 확대 타임라인 (화면 줄·소리 줄) -------------------------------------------------
// 전체 막대 아래에 썸네일을 이어 붙인 화면 줄과 소리 파형 줄. 보기 범위 S.view 는 timeline.js 로 계산한다.
// 썸네일 시트는 서버가 뒤에서 하나씩 만들고("thumbs" 알림) 오는 대로 채운다.
const viewWin = h('i', { class: 'vwin', hidden: true });   // 전체 막대 위 노란 틀
const ROW_H = 54;    // 화면 줄 높이 (px)
const WAVE_H = 34;   // 소리 줄 높이

async function loadThumbs(id) {
  const t = await call('get_thumbs', id);
  if (S.sel !== id) return;
  S.thumbs = t || false;
  zoomTl.draw();
}

async function loadWave(id) {
  const w = await call('get_waveform', id);
  if (S.sel !== id) return;
  if (!w) { S.wave = false; zoomTl.draw(); return; }
  const raw = atob(w.data);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  S.wave = { hop: w.hop, bytes };
  zoomTl.draw();
}

// 확대했을 때(칸 < 썸네일 간격)나 시트가 아직 없을 때의 썸네일: 숨은 <video> 로 미리보기 영상을 그 프레임에 맞춰
// 작은 canvas 에 그린다 (미리보기 영상은 모든 프레임이 기준 프레임이라 빠르다). 키는 원본 프레임 번호, 최대 FRAME_CACHE 장.
const FRAME_CACHE = 600;
const frameGrab = (() => {
  const v = h('video', { class: 'grab', muted: true, preload: 'auto', playsinline: true });
  v.muted = true;
  let url = null, tw = 160, th = 90, busy = false, queue = [];
  const cache = new Map();   // 프레임 번호 → canvas (넣은 순서 = 오래된 순서)
  const key = (t) => Math.round(t * fps());
  function pump() {
    if (busy || !url || !queue.length || v.readyState < 1) return;
    busy = true;
    v.currentTime = queue.shift() + 0.25 / fps();   // 프레임 경계에 딱 맞추면 앞 프레임이 나올 수 있다
  }
  v.addEventListener('loadedmetadata', pump);
  v.addEventListener('seeked', () => {
    if (!url) return;
    const c = document.createElement('canvas');
    c.width = tw; c.height = th;
    try { c.getContext('2d').drawImage(v, 0, 0, tw, th); } catch (e) { /* 아직 못 그림 */ }
    cache.set(key(v.currentTime - 0.25 / fps()), c);
    while (cache.size > FRAME_CACHE) cache.delete(cache.keys().next().value);
    busy = false;
    zoomTl.draw();
    pump();
  });
  v.addEventListener('error', () => { busy = false; queue = []; });
  return {
    attach(u, w, hgt) {
      if (u === url) return;
      this.detach();
      url = u; tw = w; th = hgt;
      if (!v.isConnected) document.body.append(v);
      v.src = u;
    },
    detach() {
      url = null; queue = []; busy = false; cache.clear();
      v.removeAttribute('src');
      v.load();
    },
    get(t) {
      const c = cache.get(key(t));
      if (c) { cache.delete(key(t)); cache.set(key(t), c); }   // 최근에 쓴 것은 뒤로
      return c || null;
    },
    // 지금 보이는 칸들만 (보기 범위가 바뀌면 대기열을 새로 바꾼다)
    want(ts) { queue = ts.filter((t) => !cache.has(key(t))); pump(); },
  };
})();

// t 가 아래 줄들에 보이게 (안 보이면 넘긴다)
function ensureVisible(t) {
  if (!S.view || t === null || !isFinite(t)) return;
  const g = zoomTl.geom();
  if (!g) return;
  const v = TL.followT(S.view, t, g.d, g.w, g.cw, fps());
  if (v.t0 !== S.view.t0 || v.spc !== S.view.spc) { S.view = v; zoomTl.draw(); }
}

const zoomTl = (() => {
  const frames = h('canvas', { class: 'zt-frames' });
  const wave = h('canvas', { class: 'zt-wave' });
  const head = h('i', { class: 'zhead', hidden: true });
  const rows = h('div', { class: 'zt-rows' }, frames, wave, head);
  const slider = h('input', { type: 'range', min: '0', max: '1000', step: '1', value: '0', 'aria-label': '확대' });
  const label = h('span', { class: 'muted' });
  const zbtn = (text, title, factor) => {
    const el = h('button', { type: 'button', class: 'btn', onclick: () => zoomBy(factor) }, text);
    el.title = title;
    return el;
  };
  const el = h('div', { class: 'ztl' }, rows,
    h('div', { class: 'zt-zoom' }, zbtn('−', '축소', 0.5), slider, zbtn('+', '확대', 2), label));
  const imgs = new Map();   // 시트 주소 → Image
  let raf = 0;

  // 지금 줄 너비·칸 너비·영상 길이 (화면에 붙기 전엔 null)
  function geom() {
    const d = rangeDur();
    const w = rows.clientWidth;
    if (!d || !w) return null;
    const t = S.thumbs;
    const cw = t ? Math.max(24, Math.round((ROW_H * t.tile_w) / t.tile_h)) : Math.round((ROW_H * 16) / 9);
    return { d, w, cw };
  }
  // 확대 기준: 재생 위치가 보이면 거기, 아니면 가운데
  function anchor(g) {
    const t = player.ready() ? player.time() : null;
    const span = (S.view.spc * g.w) / g.cw;
    return t !== null && t >= S.view.t0 && t <= S.view.t0 + span ? t : S.view.t0 + span / 2;
  }
  function zoomBy(factor, at) {
    const g = geom();
    if (!g || !S.view) return;
    S.view = TL.zoomAt(S.view, factor, at === undefined ? anchor(g) : at, g.d, g.w, g.cw, fps());
    draw();
  }
  slider.addEventListener('input', () => {
    const g = geom();
    if (!g || !S.view) return;
    const fit = TL.fitView(g.d, g.w, g.cw).spc;
    const spc = TL.sliderToSpc(Number(slider.value), Math.min(1 / fps(), fit), fit);
    zoomBy(S.view.spc / spc);
  });
  // ⌘/Ctrl+휠·트랙패드 벌리기 = 마우스 자리 기준 확대, 그냥 휠 = 좌우 이동
  rows.addEventListener('wheel', (e) => {
    const g = geom();
    if (!g || !S.view) return;
    e.preventDefault();
    const x = e.clientX - rows.getBoundingClientRect().left;
    if (e.ctrlKey || e.metaKey) {
      zoomBy(Math.exp(-e.deltaY * 0.01), TL.xToT(S.view, x, g.cw));
    } else {
      const dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      S.view = TL.clampView({ t0: S.view.t0 + (dx / g.cw) * S.view.spc, spc: S.view.spc }, g.d, g.w, g.cw, fps());
      draw();
    }
  }, { passive: false });
  // 화면 줄·소리 줄의 누르기·끌기: 손잡이는 구간 끝 ±6px
  const toT = (x, rect) => { const g = geom(); return g && S.view ? TL.xToT(S.view, x - rect.left, g.cw) : 0; };
  const endAt = (e, rect) => {
    const g = geom();
    if (!S.range || !g || !S.view) return null;
    const x = e.clientX - rect.left;
    const xs = TL.tToX(S.view, S.range.start, g.cw), xe = TL.tToX(S.view, S.range.end, g.cw);
    const ds = Math.abs(x - xs), de = Math.abs(x - xe);
    if (Math.min(ds, de) > 6) return null;
    return ds < de || (ds === de && x < (xs + xe) / 2) ? 'start' : 'end';
  };
  rangePointer(frames, toT, endAt);
  rangePointer(wave, toT, endAt);

  function sheetImg(url) {
    let img = imgs.get(url);
    if (!img) {
      img = new Image();
      img.onload = () => draw();
      img.src = url;
      imgs.set(url, img);
    }
    return img.complete && img.naturalWidth ? img : null;
  }

  function colors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    return { cut: v('--cut'), accent: v('--accent'), fg: v('--fg'), soft: v('--soft'), bg: v('--bg'),
      muted: v('--muted'), back: v('--restored') };
  }
  function fit(cv, w, hgt) {
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(hgt * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(hgt * dpr);
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }
  // 두 줄에 같이 그리는 것: 잘린 곳 빗금, 되살린 곳, 구간, 찍어 둔 시작점
  function overlays(ctx, g, hgt, c) {
    const r = S.resultFor === S.sel ? S.result : null;
    const X = (t) => TL.tToX(S.view, t, g.cw);
    const span = [S.view.t0, S.view.t0 + (S.view.spc * g.w) / g.cw];
    const vis = (a, b) => b > span[0] && a < span[1];
    ctx.save();
    for (const [a, b] of merge((r && r.cuts) || [])) {
      if (!vis(a, b)) continue;
      const x = X(a), w = Math.max(1, X(b) - x);
      ctx.fillStyle = c.cut + '55';
      ctx.fillRect(x, 0, w, hgt);
      ctx.strokeStyle = c.cut + 'aa';
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let k = -hgt; k < w; k += 8) { ctx.moveTo(x + k, hgt); ctx.lineTo(x + k + hgt, 0); }
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, 0, w, hgt);
      ctx.clip();
      ctx.beginPath();
      for (let k = -hgt; k < w; k += 8) { ctx.moveTo(x + k, hgt); ctx.lineTo(x + k + hgt, 0); }
      ctx.stroke();
      ctx.restore();
    }
    const backs = [...((r && r.cut_items) || []).filter((it) => it.restored), ...restoreOps(r)];
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = c.back;
    for (const it of backs) if (vis(it.start, it.end)) ctx.strokeRect(X(it.start) + 0.5, 0.5, X(it.end) - X(it.start) - 1, hgt - 1);
    ctx.setLineDash([]);
    const R = S.range;
    if (R && vis(R.start, R.end)) {
      const x1 = X(R.start), x2 = X(R.end);
      ctx.fillStyle = c.accent + '33';
      ctx.fillRect(x1, 0, x2 - x1, hgt);
      ctx.strokeStyle = c.accent;
      ctx.lineWidth = 2;
      ctx.strokeRect(x1, 1, x2 - x1, hgt - 2);
      for (const [which, x] of [['start', x1], ['end', x2]]) {
        ctx.fillStyle = c.accent;
        ctx.fillRect(x - 3, 2, 6, hgt - 4);
        if (which === S.rangeEnd) {   // ←/→ 로 옮길 끝
          ctx.strokeStyle = c.bg;
          ctx.lineWidth = 2;
          ctx.strokeRect(x - 4, 1, 8, hgt - 2);
        }
      }
    }
    if (S.mark !== null && vis(S.mark, S.mark)) {
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = c.accent;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(X(S.mark), 0);
      ctx.lineTo(X(S.mark), hgt);
      ctx.stroke();
    }
    ctx.restore();
  }

  function paint() {
    raf = 0;
    const r = S.resultFor === S.sel ? S.result : null;
    const g = geom();
    const showFrames = !!(r && r.preview && r.preview.video) && S.thumbs !== false;
    const showWave = S.wave !== false;
    frames.hidden = !showFrames;
    wave.hidden = !showWave;
    el.hidden = !r || !g || (!showFrames && !showWave);
    if (el.hidden || !g) { viewWin.hidden = true; return; }
    S.view = TL.clampView(S.view || TL.fitView(g.d, g.w, g.cw), g.d, g.w, g.cw, fps());
    const c = colors();
    const v = S.view;
    if (showFrames) {
      const ctx = fit(frames, g.w, ROW_H);
      ctx.clearRect(0, 0, g.w, ROW_H);   // 바탕색이 반투명이라 지우고 칠한다
      ctx.fillStyle = c.soft;
      ctx.fillRect(0, 0, g.w, ROW_H);
      const T = S.thumbs;
      frameGrab.attach(r.preview.video, T ? T.tile_w : 160, T ? T.tile_h : 90);
      const fine = !T || v.spc < T.step * 0.999;   // 칸이 썸네일 간격보다 좁으면 프레임을 직접 그린다
      const need = [];
      for (const cell of TL.cells(v, g.d, g.w, g.cw)) {
        const sc = T ? TL.sheetCell(cell.t, T) : null;
        const url = sc && T.sheets[sc.sheet];
        const img = url && sheetImg(url);
        const fc = fine || !img ? frameGrab.get(cell.t) : null;
        if (fc) ctx.drawImage(fc, cell.x, 0, g.cw, ROW_H);
        else if (img) ctx.drawImage(img, sc.col * T.tile_w, sc.row * T.tile_h, T.tile_w, T.tile_h, cell.x, 0, g.cw, ROW_H);
        if (!fc && (fine || !img)) need.push(cell.t);
        ctx.fillStyle = c.bg;
        ctx.fillRect(cell.x + g.cw - 1, 0, 1, ROW_H);   // 칸 사이 선
      }
      frameGrab.want(need);
      overlays(ctx, g, ROW_H, c);
    }
    if (showWave) {
      const ctx = fit(wave, g.w, WAVE_H);
      ctx.clearRect(0, 0, g.w, WAVE_H);   // 바탕색이 반투명이라 지우고 칠한다
      ctx.fillStyle = c.soft;
      ctx.fillRect(0, 0, g.w, WAVE_H);
      if (S.wave) {
        ctx.fillStyle = c.muted;
        const { hop, bytes } = S.wave;
        for (let x = 0; x < g.w; x++) {
          const a = TL.xToT(v, x, g.cw), b = TL.xToT(v, x + 1, g.cw);
          if (a >= g.d) break;
          const hh = (TL.waveMax(bytes, hop, a, b) / 255) * (WAVE_H - 2);
          if (hh > 0.5) ctx.fillRect(x, (WAVE_H - hh) / 2, 1, hh);
        }
      }
      overlays(ctx, g, WAVE_H, c);
    }
    // 확대 조절·노란 틀
    const fitSpc = TL.fitView(g.d, g.w, g.cw).spc;
    if (document.activeElement !== slider) slider.value = String(Math.round(TL.spcToSlider(v.spc, Math.min(1 / fps(), fitSpc), fitSpc)));
    const frm = v.spc * fps();
    label.textContent = frm < 1.5 ? '1칸 = 1프레임' : v.spc < 1 ? `1칸 = ${Math.round(frm)}프레임`
      : `1칸 = ${v.spc < 10 ? v.spc.toFixed(1) : Math.round(v.spc)}초`;
    const span = (v.spc * g.w) / g.cw;
    viewWin.hidden = span >= g.d * 0.999;
    viewWin.style.left = `${(v.t0 / g.d) * 100}%`;
    viewWin.style.width = `${(Math.min(span, g.d - v.t0) / g.d) * 100}%`;
    drawHeadLine(player.ready() ? player.time() : null);
  }
  function drawHeadLine(t) {
    const g = geom();
    if (!g || !S.view || t === null) { head.hidden = true; return; }
    const x = TL.tToX(S.view, t, g.cw);
    head.hidden = x < 0 || x > g.w;
    head.style.left = `${x}px`;
  }
  function draw() { if (!raf) raf = requestAnimationFrame(paint); }

  window.addEventListener('resize', draw);
  return {
    el, geom, draw,
    // 재생 위치가 움직일 때 (재생 중 보기 밖으로 나가면 넘긴다)
    head(t, playing) {
      if (playing && S.view) {
        const g = geom();
        if (g && (t < S.view.t0 || t > S.view.t0 + (S.view.spc * g.w) / g.cw)) { ensureVisible(t); return; }
      }
      drawHeadLine(t);
    },
    reset() { imgs.clear(); frameGrab.detach(); },
  };
})();

async function rangeOp(kind) {
  const j = selected();
  const R = S.range;
  if (!j || S.rangeBusy || !R) return;
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
  if (e.key !== 'Escape' || (!S.range && S.mark === null) || S.editing || tlDrag) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
  S.range = null; S.mark = null; S.rangeErr = false;
  drawRange();
});
// ←/→: 구간이 있으면 마지막으로 만진 끝을, 없으면 재생 위치를 1프레임 (Shift 와 함께 10프레임).
// I/O: 지금 위치를 구간 시작·끝으로 (`여기서 시작`·`여기서 끝`)
document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing || S.editing || tlDrag || typingIn(e.target)) return;
  if (rangeBar.el.hidden || !rangeDur() || !player.ready()) return;   // 결과 화면에서만
  const k = e.key;
  if (k === 'ArrowLeft' || k === 'ArrowRight') {
    e.preventDefault();
    const n = (k === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 10 : 1);
    if (S.range) stepEnd(S.rangeEnd, n);
    else { const t = snapT(player.time() + n / fps(), rangeDur()); player.seek(t); ensureVisible(t); }
  } else if ((k === 'i' || k === 'I' || k === 'o' || k === 'O') && !e.shiftKey && !S.rangeBusy) {
    e.preventDefault();
    pickEnd(k.toLowerCase() === 'i' ? 'start' : 'end', player.time());
  }
});

const KIND = { silence: '말 없는 구간', filler: '망설임', habit: '말버릇', manual: '직접 자름', restore_op: '직접 되살림' };
// 잘린 곳 목록의 종류 탭
const CUT_KINDS = [['all', '전체'], ['silence', '말 없는 구간'], ['filler', '망설임'], ['habit', '말버릇'],
  ['manual', '직접 자름'], ['restore_op', '직접 되살림']];

// 잘린 곳 목록: 하나씩 되살리거나 다시 자를 수 있다 (처리 없이 바로 반영, 저장 때 쓰임).
// 타임라인에서 직접 자른 곳(manual)·직접 되살린 구간은 그 작업 기록을 지워서 되돌린다.
// 전체에선 줄마다 종류 뱃지를 붙인다
function cutRows(j, items) {
  const all = S.cutKind === 'all';
  const editable = HAS_RESULT.has(j.status);
  const rows = items.map((c) => {
    const op = c.kind === 'manual' || c.kind === 'restore_op';
    const back = op ? c.kind === 'restore_op' : c.restored;
    const name = c.word ? `“${c.word}”` : all ? '' : KIND[c.kind] || '';
    const undo = op ? () => removeOp(j.id, c.op) : () => setRestored(j.id, c, !c.restored);
    return h('div', { class: 'opt row' + (c.restored ? ' undone' : '') + (c.kind === 'restore_op' ? ' op-back' : ''),
      onclick: () => player.clip(c.start, c.end, true) },
      h('span', {}, h('span', { class: 'w' }, clock(c.start) + (name ? ` ${name}` : '')),
        all ? h('span', { class: `tag k-${c.kind}` }, KIND[c.kind] || '') : null,
        h('span', { class: 'sec' }, `${(c.end - c.start).toFixed(1)}초`)),
      h('button', { type: 'button', class: 'btn' + (back ? ' on' : ''), disabled: !editable,
        onclick: (e) => { e.stopPropagation(); undo(); } }, back ? '다시 자르기' : '되살리기'));
  });
  return rows.length ? rows : [h('div', { class: 'opt muted' }, h('span', {}, '없음'))];
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
    tab('cuts', `잘린 곳 ${items.length}` + (n ? ` · 되살린 곳 ${n}` : '')),
    tab('subs', caps ? `자막 ${caps.filter((c) => !c.empty).length}` : '자막'))];
  if (S.tab === 'subs') {
    syncFindBar(HAS_RESULT.has(j.status));
    out.push(findBar.el, h('div', { class: 'cut-list subs', 'data-tab': 'subs' }, (caps || []).map((c) => capRow(j, c))));
    return out;
  }
  const every = [...items, ...restoreOps(r).map((o) => ({ start: o.start, end: o.end, kind: 'restore_op', op: o.i }))]
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const of = (k) => (k === 'all' ? every : every.filter((c) => c.kind === k));
  out.push(h('div', { class: 'kinds', role: 'tablist' }, CUT_KINDS.map(([k, text]) =>
    h('button', { type: 'button', class: S.cutKind === k ? 'on' : null,
      onclick: () => { if (S.cutKind !== k) { S.cutKind = k; renderResult(); } } }, `${text} ${of(k).length}`))));
  out.push(h('div', { class: 'cut-list', 'data-tab': `cuts:${S.cutKind}` }, cutRows(j, of(S.cutKind))));
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
    if (where === 'over' && player.playing()) return;   // 재생 중엔 보기만
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
  const now = () => (player.ready() ? player.time() : null);
  const part = (label, t, which) => {
    const go = (v) => (v === null ? null : which === 'start' ? set(v, null) : set(null, v));
    return h('span', { class: 'lt' }, `${label} `, h('b', {}, clock1(t)),
      h('button', { type: 'button', class: 'btn', disabled: !can, onclick: () => go(t - 0.1) }, '◀0.1'),
      h('button', { type: 'button', class: 'btn', disabled: !can, onclick: () => go(t + 0.1) }, '0.1▶'),
      h('button', { type: 'button', class: 'btn', disabled: !can || !player.ready(), onclick: () => go(now()) }, '지금 위치로'));
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
    if (r && r.captions) applyCaps(j.id, { captions: r.captions, edits_dropped: r.edits_dropped, history: r.history });
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

// ---- 미리보기 재생기 -------------------------------------------------------
// mse: 남길 구간을 이어 붙인 MediaSource 재생기 (시간·기본 컨트롤은 결과 기준). 멈춘 채 잘린 구간 안으로 옮기면
//      원본 보기(조각 영상 파일을 원본 시간 그대로, 소리 없음)를 대신 보이고, 재생하면 그 구간 끝부터 이어 붙인 쪽으로 돌아간다.
// fallback: MediaSource 를 못 쓰거나 잘린 소리가 아직 없을 때 — 원본 보기 영상 + 전체 소리를 같이 틀고 잘린 구간을 건너뛴다.
// legacy: 옛 preview.mp4 만 있는 작업 — 그 파일 하나로 건너뛰기 재생.
// 화면이 쓰는 시간(time·seek·clip·자막·타임라인)은 모두 원본 기준이다.
const player = {
  mode: null, key: null, p: null, gap: null, map: null, peek: false, withAud: false,
  ranges: [], win: null, raf: 0, ownSeek: false, seq: 0, wait: null,
  broken: new Set(),   // 이어 붙이기에 실패한 잘린 소리 — 다시 시도하지 않고 예전 방식으로 튼다
  box: null, mse: null, orig: null, aud: null,
  note: h('div', { class: 'pv-note', hidden: true }, '미리보기 소리를 맞추는 중…'),
  head: h('i', { class: 'head', style: 'left:0' }),
  sub: h('div', { class: 'subline empty' }), subKey: null,

  slot() {
    if (!this.box) {
      const mse = h('video', { controls: true, preload: 'auto', playsinline: true });
      const orig = h('video', { controls: true, preload: 'auto', playsinline: true, hidden: true });
      for (const v of [mse, orig]) {
        v.addEventListener('timeupdate', () => this.tick());
        v.addEventListener('play', () => this.onPlay(v));
        v.addEventListener('pause', () => { if (v === this.el() && this.withAud) this.aud.pause(); });
        v.addEventListener('seeked', () => this.drawHead());
        v.addEventListener('seeking', () => {
          if (v !== this.el()) return;
          if (!this.ownSeek) this.win = null;   // 사용자가 직접 옮기면 들어보기 구간을 끝낸다
          this.ownSeek = false;
          if (this.withAud) this.aud.currentTime = v.currentTime;
        });
        v.addEventListener('loadedmetadata', () => { if (!(S.result && S.result.duration)) renderResult(true); });
      }
      this.mse = mse; this.orig = orig; this.aud = h('audio', { preload: 'auto' });
      this.box = h('div', { class: 'pv' }, mse, orig, this.note);
    }
    return this.box;
  },
  // 지금 보이는(조작하는) 요소
  el() {
    if (!this.mode) return null;
    return this.mode === 'mse' && !this.peek ? this.mse : this.orig;
  },
  show() {
    this.mse.hidden = this.el() !== this.mse;
    this.orig.hidden = this.el() !== this.orig;
  },
  attach(r) {
    this.slot();
    const p = r.preview;
    this.ranges = merge(r.cuts || []);
    this.note.hidden = !(p && p.pending);
    let want;
    if (!p) want = { mode: 'legacy', key: `legacy|${r.preview_url}`, url: r.preview_url };
    else if (p.cut_audio && p.cut_index && !this.broken.has(p.cut_audio)) want = { mode: 'mse', key: `mse|${p.video}|${p.cut_audio}` };
    else if (p.pending && this.mode === 'mse') want = null;   // 새 소리가 준비될 때까지 지금 미리보기를 그대로 튼다
    else want = { mode: 'fallback', key: `fallback|${p.video}|${p.audio_full}` };
    if (want && want.key !== this.key) this.open(want, p);
    this.drawHead();
  },
  open(want, p) {
    // 다시 만든 미리보기는 듣던 원본 위치에서 이어서 재생한다 (불러오는 중에 또 바뀌면 처음 기억한 위치)
    const keep = this.mode && !this.wait ? { t: this.time(), play: this.playing() } : this.wait;
    this.close();
    const seq = ++this.seq;
    this.mode = want.mode; this.key = want.key; this.p = p;
    this.show();
    if (want.mode === 'mse') { this.startMse(p, keep, seq); return; }
    const o = this.orig;
    this.withAud = want.mode === 'fallback' && !!p.video;
    o.muted = this.withAud;
    o.src = want.mode === 'legacy' ? want.url : (p.video || p.audio_full);
    if (this.withAud) this.aud.src = p.audio_full;
    if (keep) {
      this.wait = keep;
      o.addEventListener('loadedmetadata', () => {
        if (seq !== this.seq) return;
        this.wait = null;
        if (keep.t > 0) this.move(Math.min(keep.t, this.duration() || keep.t));
        if (keep.play) o.play().catch(() => {});
      }, { once: true });
    }
  },
  async startMse(p, keep, seq) {
    this.wait = keep || { t: 0, play: false, fresh: true };
    try {
      const json = async (u) => {
        const res = await fetch(u);
        if (!res.ok) throw new Error(`색인을 받지 못했습니다 (${res.status})`);
        return res.json();
      };
      const [vi, ci] = await Promise.all([p.video ? json(p.video_index) : null, json(p.cut_index)]);
      if (seq !== this.seq) return;
      const gap = this.gap = new Gapless(this.mse);
      gap.onerror = (e) => this.failMse(p.cut_audio, seq, e);
      await gap.load({ videoUrl: p.video, videoIndex: vi, audioUrl: p.cut_audio, audioIndex: ci });
      if (seq !== this.seq) return;
      this.map = timeMap(ci.keeps || []);
      const w = this.wait;
      this.wait = null;
      // 처음 열 땐 결과 처음에서 (원본 0초가 잘린 구간이어도 원본 보기로 시작하지 않는다)
      if (w.play) { this.move(this.map.toOut(w.t)); this.mse.play().catch(() => {}); } else if (!w.fresh) this.seek(w.t);
      this.drawHead();
    } catch (e) {
      this.failMse(p.cut_audio, seq, e);
    }
  },
  // 이어 붙이기 실패: 그 소리는 다시 시도하지 않고 같은 원본 위치에서 예전 방식(건너뛰기)으로 튼다
  failMse(url, seq, e) {
    if (seq !== this.seq) return;
    console.warn('미리보기: 예전 방식으로 재생', e);
    this.broken.add(url);
    const keep = this.wait || { t: this.time(), play: this.playing() };
    this.close();
    this.mode = null; this.key = null; this.wait = keep;
    if (S.result && S.resultFor === S.sel) this.attach(S.result);
  },
  close() {
    cancelAnimationFrame(this.raf);
    this.seq++;
    if (this.gap) { this.gap.destroy(); this.gap = null; }
    for (const v of [this.mse, this.orig, this.aud]) {
      if (v && v.hasAttribute('src')) { v.pause(); v.removeAttribute('src'); v.load(); }
    }
    this.map = null; this.peek = false; this.withAud = false; this.win = null; this.wait = null;
  },
  detach() {
    if (this.box) this.close();
    this.mode = null; this.key = null; this.p = null; this.ranges = [];
    this.note.hidden = true;
  },
  ready() { return !!this.mode; },
  // 원본 기준 지금 시간
  time() {
    if (!this.mode) return 0;
    if (this.wait) return this.wait.t;
    if (this.mode !== 'mse' || this.peek) return this.orig.currentTime;
    return this.map ? this.map.toOrig(this.mse.currentTime) : 0;
  },
  playing() {
    const v = this.el();
    return !!v && !v.paused && !v.ended;
  },
  duration() {
    if (this.mode === 'mse') return this.map ? this.map.toOrig(this.map.total) : 0;
    return this.orig && isFinite(this.orig.duration) ? this.orig.duration : 0;
  },
  // 보이는 요소의 시간으로 옮긴다 (mse 면 결과 시간)
  move(t) {
    const v = this.el();
    this.ownSeek = true;
    v.currentTime = t;
    if (this.withAud) this.aud.currentTime = t;
  },
  setPeek(on) {
    on = on && !!(this.p && this.p.video);   // 영상 없는 입력은 원본 보기가 없다
    if (on && !this.orig.hasAttribute('src')) { this.orig.muted = true; this.orig.src = this.p.video; }
    this.peek = on;
    this.show();
  },
  seek(t) {
    if (!this.mode) return;
    this.win = null;
    t = Math.max(0, t);
    if (this.wait) { this.wait.t = t; this.wait.fresh = false; return; }   // 다 열면 그 자리로
    if (this.mode !== 'mse') { this.move(t); return; }
    if (!this.map) return;
    if (!this.playing() && this.map.inCut(t) && this.p.video) {
      this.setPeek(true);
      this.move(t);
      this.mse.currentTime = this.map.toOut(t);   // 재생을 누르면 바로 나오게 미리 채워 둔다
    } else {
      this.setPeek(false);
      this.move(this.map.toOut(t));
    }
  },
  clip(start, end, skip) {
    if (!this.mode || this.wait) return;
    const from = Math.max(0, start - 2);
    if (this.mode === 'mse') {
      if (!this.map) return;
      this.setPeek(false);
      this.move(this.map.toOut(from));
      this.win = { to: this.map.toOut(end + 2), skip: true };
    } else {
      this.win = { to: Math.min(this.duration() || end + 2, end + 2), skip };
      this.move(from);
    }
    this.el().play().catch(() => {});
  },
  onPlay(v) {
    if (v !== this.el()) { v.pause(); return; }
    if (this.peek) {
      // 원본 보기에서 재생: 이어 붙인 재생기로 돌아가 그 잘린 구간 끝부터
      const t = v.currentTime;
      v.pause();
      this.setPeek(false);
      if (this.map) this.move(this.map.toOut(t));
      this.mse.play().catch(() => {});
      return;
    }
    if (this.withAud) { this.aud.currentTime = v.currentTime; this.aud.play().catch(() => {}); }
    this.loop();
  },
  loop() {
    cancelAnimationFrame(this.raf);
    const step = () => {
      this.tick();
      if (this.playing()) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  },
  tick() {
    const v = this.el();
    if (!v) return;
    this.drawHead();
    if (v.paused) return;
    const t = v.currentTime;
    if (this.win) {
      if (t >= this.win.to) { v.pause(); this.win = null; return; }
      if (!this.win.skip) return;
    }
    if (this.mode === 'mse') return;   // 이미 잘린 결과를 튼다
    if (this.withAud && Math.abs(this.aud.currentTime - t) > 0.3) this.aud.currentTime = t;
    for (const [a, b] of this.ranges) {
      if (a > t) break;
      if (t >= a && t < b - 0.01) { this.move(Math.min(b, this.duration() || b)); break; }
    }
  },
  drawHead() {
    const d = (S.result && S.result.duration) || this.duration();
    const t = this.time();
    this.head.style.left = d ? `${Math.min(100, (t / d) * 100)}%` : '0';
    zoomTl.head(t, this.playing());
    this.drawSub(t);
  },
  // 영상 아래 자막: 지금 시간의 줄 (고친 줄 시간 반영). 줄이 바뀔 때만 다시 만든다
  drawSub(t) {
    if (S.editing) return;
    const caps = (S.capsFor === S.sel && S.caps) || [];
    const line = this.mode ? caps.find((c) => !c.empty && c.start <= t && t < c.end) : null;
    const key = line ? `${line.key}|${S.capsVersion}` : '';
    if (key === this.subKey) return;
    this.subKey = key;
    this.sub.classList.toggle('empty', !line);
    this.sub.replaceChildren(...(line ? line.words.map((w) => wordEl(w, 'over')) : []));
  },
  // 개발자 도구에서 재는 용도: player.debug()
  debug() {
    const b = this.mse ? this.mse.buffered : null;
    return { mode: this.mode, peek: this.peek, pending: !this.note.hidden, time: this.time(),
      out: this.mode === 'mse' && this.mse ? this.mse.currentTime : null,
      buffered: b ? Array.from({ length: b.length }, (_, i) => [b.start(i), b.end(i)]) : [] };
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
  else if (event === 'preview') { if (data.id && data.id === S.sel) loadResult(data.id); }   // 잘린 소리를 다 만들었다
  else if (event === 'thumbs') { if (data.id && data.id === S.sel) loadThumbs(data.id); }    // 썸네일 시트 하나가 됐다
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
