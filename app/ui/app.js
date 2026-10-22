'use strict';
/* youtube-edit 화면. 파이썬 Api 는 pywebview.api.*, 파이썬 알림은 window.onAppEvent(event, data). */

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

const OPTS = [
  { key: 'filler', label: '망설임 소리', sub: '아·어·음…', choices: [['keep', '유지'], ['remove', '제거']] },
  { key: 'habit', label: '말버릇', sub: '그니까·약간·이제…', choices: [['keep', '유지'], ['some', '일부'], ['all', '모두']] },
  { key: 'silence', label: '말 없는 구간', choices: [['keep', '유지'], ['remove', '제거']] },
  { key: 'denoise', label: '배경 잡음', choices: [['keep', '유지'], ['remove', '제거']] },
];
const ADV = [
  { key: 'loudnorm', label: '음량 맞추기', sub: '유튜브 기준 크기(-14 LUFS)로 맞춤', choices: [['keep', '유지'], ['match', '맞춤']] },
  { key: 'subs', label: '영상 안 자막', sub: 'mp4 안에 켜고 끄는 자막 트랙. SRT 파일은 항상 만듦', choices: [['omit', '빼기'], ['embed', '넣기']] },
  { key: 'quality', label: '화질', sub: '자동: 1080p30 8Mbps · 4K30 약 32Mbps', choices: [['low', '작게'], ['auto', '자동'], ['high', '높게']] },
];
const DEFAULTS = { filler: 'remove', habit: 'some', silence: 'remove', denoise: 'remove', loudnorm: 'match', subs: 'embed', quality: 'auto' };
const FAST = new Set(['filler', 'habit', 'silence']);
const BUSY = new Set(['processing', 'saving']);
const HAS_RESULT = new Set(['review', 'saved']);
const ROWS_FIRST = 5;

const S = {
  jobs: [], sel: null, options: null,
  result: null, resultFor: null, resultKey: null,
  showAll: false, changedKey: null, saveDir: '', advOpen: false, update: null,
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
  S.showAll = false; S.changedKey = null;
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

async function loadResult(id) {
  const r = await call('get_result', id);
  if (S.sel !== id || !r) return;
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

function renderQueue() {
  const empty = S.jobs.length === 0;
  $('empty').hidden = !empty;
  $('work').hidden = empty;
  const q = $('queue');
  q.replaceChildren(
    ...S.jobs.map((j) => {
      const busy = BUSY.has(j.status);
      return h('div', { class: 'qi' + (j.id === S.sel ? ' cur' : ''), onclick: () => select(j.id), title: j.name },
        h('span', { class: 'name' }, j.name),
        h('span', { class: `st ${j.status}`, title: j.status === 'failed' ? (j.error || '') : null }, statusText(j)),
        busy ? h('span', { class: 'mini' }, h('i', { style: `width:${pct(j.progress)}%` })) : null,
        h('button', { type: 'button', class: 'x', title: '빼기', onclick: (e) => { e.stopPropagation(); removeJob(j); } }, '✕'));
    }),
    h('div', { class: 'qi add', onclick: pickFiles }, '＋ 끌어다 놓거나 ', h('u', {}, '파일 선택')));
}

function segRow(def, value, disabled, onPick, changed) {
  return h('div', { class: 'opt' + (disabled ? ' disabled' : '') + (changed ? ' changed' : '') },
    h('span', {}, def.label, def.sub ? h('small', {}, def.sub) : null),
    h('span', { class: 'seg' }, def.choices.map(([v, text]) =>
      h('button', { type: 'button', class: v === value ? 'sel' : null, disabled: disabled || null,
        onclick: () => onPick(def.key, v) }, text))));
}

function renderOptions() {
  const j = selected();
  const o = S.options || DEFAULTS;
  const disabled = !j || !S.options || BUSY.has(j.status) || j.status === 'missing';
  $('opt-title').textContent = j ? `${j.name} 옵션` : '';
  $('options').replaceChildren(...OPTS.map((d) => segRow(d, o[d.key], disabled, setOption, S.changedKey === d.key)));
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
  const key = j ? [j.id, j.status, j.error, S.resultVersion, S.showAll].join('|') : '';
  if (!force && key === S.drawn) return;
  S.drawn = key;
  if (!j) { box.replaceChildren(); return; }
  const r = S.resultFor === j.id ? S.result : null;
  box.classList.toggle('busy', !!r && BUSY.has(j.status));
  const parts = [];
  if (j.status === 'failed') {
    parts.push(h('div', { class: 'err' }, '실패' + (j.error ? ` — ${j.error}` : '')));
  }
  if (r) parts.push(...resultParts(j, r));
  // 재생기는 그대로 두고 나머지만 다시 그린다 (재생 위치 유지)
  box.replaceChildren(...parts);
  if (r) player.attach(r);
}

function resultParts(j, r) {
  const s = r.summary || {};
  const out = [];
  out.push(h('div', { class: 'summary' },
    h('b', { class: 'stat' }, `${clock(s.orig_sec)} → ${clock(s.final_sec)}`), ' · ',
    h('span', { class: 'stat' }, `${s.cuts || 0}곳 잘림`), ' · ',
    h('span', { class: 'stat' }, `−${Math.round(Number(s.removed_sec) || 0)}초`)));
  out.push(player.slot());
  out.push(timeline(r));
  out.push(h('div', { class: 'legend' },
    h('b', { class: 'cut' }, '■ 잘린 곳'), ' · ', h('b', { class: 'changed' }, '■ 달라진 곳')));
  if (r.changes) out.push(...changeRows(r.changes));
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
    out.push(h('div', { class: 'opt' },
      h('span', {}, `${clock(row.start)} “${row.word}” `,
        h('span', { class: added ? 'tag add' : 'tag back' }, added ? '추가 잘림' : '다시 살림')),
      h('span', { class: 'play' },
        h('button', { type: 'button', class: 'link', onclick: () => player.clip(row.start, row.end, false) }, '▶ 원본'),
        ' · ',
        h('button', { type: 'button', class: 'link', onclick: () => player.clip(row.start, row.end, true) }, '▶ 수정본'))));
  }
  if (!S.showAll && rows.length > ROWS_FIRST) {
    out.push(h('div', { class: 'opt more', onclick: () => { S.showAll = true; renderResult(true); } }, h('span', {}, '+ 더 보기'), h('span')));
  }
  return out;
}

// ---- 미리보기 재생기: 잘린 구간을 건너뛰며 재생 ---------------------------
const player = {
  video: null, url: null, ranges: [], win: null, raf: 0,
  head: h('i', { class: 'head', style: 'left:0' }),

  slot() {
    if (!this.video) {
      const v = h('video', { controls: true, preload: 'metadata', playsinline: true });
      v.addEventListener('timeupdate', () => this.tick());
      v.addEventListener('play', () => this.loop());
      v.addEventListener('seeked', () => this.drawHead());
      v.addEventListener('loadedmetadata', () => { if (!(S.result && S.result.duration)) renderResult(true); });
      this.video = v;
    }
    return this.video;
  },
  attach(r) {
    const v = this.slot();
    if (this.url !== r.preview_url) { this.url = r.preview_url; v.src = r.preview_url; }
    this.ranges = merge(r.cuts || []);
    this.drawHead();
  },
  detach() {
    if (this.video) { this.video.pause(); this.video.removeAttribute('src'); this.video.load(); }
    this.video = null; this.url = null; this.ranges = []; this.win = null;
  },
  duration() { return this.video && isFinite(this.video.duration) ? this.video.duration : 0; },
  seek(t) { if (!this.video) return; this.win = null; this.video.currentTime = Math.max(0, t); },
  clip(start, end, skip) {
    const v = this.video;
    if (!v) return;
    const from = Math.max(0, start - 2);
    const to = Math.min(this.duration() || end + 2, end + 2);
    this.win = { to, skip };
    v.currentTime = from;
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
      if (t >= a && t < b - 0.01) { v.currentTime = Math.min(b, this.duration() || b); break; }
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
  if (!total) { $('cover-bar').style.width = '0'; $('cover-text').textContent = ''; return; }
  const now = performance.now() / 1000;
  dl.samples.push([now, done]);
  while (dl.samples.length > 2 && now - dl.samples[0][0] > 20) dl.samples.shift();
  let eta = '';
  const [t0, d0] = dl.samples[0];
  if (now - t0 >= 2 && done > d0 && done < total) {
    const left = (total - done) / ((done - d0) / (now - t0));
    eta = ` · 약 ${Math.max(1, Math.round(left / 60))}분 남음`;
  }
  $('cover-bar').style.width = `${Math.min(100, (done / total) * 100)}%`;
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
    $('up-state').textContent = '받는 중…';
    const r = await call('install_update');
    if (!r || !r.started) $('up-state').textContent = '';
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
