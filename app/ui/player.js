/* 끊김 없는 미리보기: 남길 구간의 영상 조각과 파이프라인이 잘라 둔 소리를 MediaSource 로 이어 붙인다.
   재생기 시간은 결과(완성 mp4) 기준이고, 화면의 원본 시간과는 timeMap 으로 바꾼다.
   app.js 의 전역은 쓰지 않는다 (node 로 순수 함수를 시험한다). */
'use strict';

const BEHIND = 10;       // 재생 위치 뒤로 남겨 둘 초
const AHEAD = 60;        // 앞으로 넣어 둘 초
const AHEAD_MIN = 20;    // QuotaExceededError 가 나면 앞 범위를 이만큼으로 줄인다
const MERGE_MAX = 8;     // 한 번에 받아 넣을 이어진 조각 수

// 남길 구간 [[s, e], …](원본 초) → 원본↔결과 시간 변환
function timeMap(keeps) {
  const ks = [];
  let o = 0;
  for (const [s, e] of keeps || []) {
    if (!(e > s)) continue;
    ks.push({ s, e, o });
    o += e - s;
  }
  const total = o;
  return {
    total,
    // 잘린 구간 안의 원본 시간은 그 구간 끝(다음 남길 구간 시작), 마지막 잘린 구간이면 total
    toOut(t) {
      for (const k of ks) {
        if (t < k.s) return k.o;
        if (t <= k.e) return k.o + (t - k.s);
      }
      return total;
    },
    // 구간 경계의 결과 시간은 다음 남길 구간의 시작으로 본다. total 이상이면 마지막 남길 구간 끝
    toOrig(out) {
      for (const k of ks) if (out < k.o + (k.e - k.s)) return k.s + Math.max(0, out - k.o);
      return ks.length ? ks[ks.length - 1].e : 0;
    },
    inCut(t) { return !ks.some((k) => k.s <= t && t < k.e); },
  };
}

// 조각 i 의 끝 시간 = 다음 조각 시작, 마지막은 무한
const fragEnd = (frags, i) => (i + 1 < frags.length ? frags[i + 1][0] : Infinity);
const overlaps = (frags, i, s, e) => !(fragEnd(frags, i) <= s - 1e-6 || frags[i][0] >= e);

// [s, e) 와 겹치는 조각의 바이트 범위 [start, end] (Range 머리처럼 끝 포함)
function fragsFor(frags, s, e) {
  const out = [];
  for (let i = 0; i < frags.length; i++) if (overlaps(frags, i, s, e)) out.push([frags[i][1], frags[i][2] - 1]);
  return out;
}

// 영상 넣을 목록: 남길 구간마다 걸친 조각, timestampOffset·appendWindow, 결과 시간에서 차지하는 범위
function planVideo(frags, keeps, frame) {
  const out = [];
  let o = 0;
  (keeps || []).forEach(([s, e], keep) => {
    if (!(e > s)) return;
    for (let i = 0; i < frags.length; i++) {
      if (!overlaps(frags, i, s, e)) continue;
      out.push({ keep, a: frags[i][1], b: frags[i][2] - 1, off: o - s, ws: o, we: o + (e - s) + frame,
        from: o + Math.max(frags[i][0], s) - s, to: o + Math.min(fragEnd(frags, i), e) - s });
    }
    o += e - s;
  });
  return out;
}

// 소리 넣을 목록: 이미 잘린 소리라 조각 시간 = 결과 시간. 마지막 조각은 결과 길이(total)에서 끝낸다
function planAudio(frags, total) {
  const end = total > 0 ? total : Infinity;
  return frags.map((f, i) => ({ keep: 0, a: f[1], b: f[2] - 1, from: f[0], to: Math.min(fragEnd(frags, i), end) }));
}

async function fetchRange(url, a, b) {
  const res = await fetch(url, { headers: { Range: `bytes=${a}-${b}` } });
  if (!res.ok) throw new Error(`미리보기 조각을 받지 못했습니다 (${res.status})`);
  return res.arrayBuffer();
}

function sbWait(sb, start) {
  return new Promise((res, rej) => {
    const done = () => { sb.removeEventListener('updateend', done); sb.removeEventListener('error', bad); res(); };
    const bad = () => { sb.removeEventListener('updateend', done); sb.removeEventListener('error', bad); rej(new Error('SourceBuffer 오류')); };
    sb.addEventListener('updateend', done);
    sb.addEventListener('error', bad);
    try { start(); } catch (e) { sb.removeEventListener('updateend', done); sb.removeEventListener('error', bad); rej(e); }
  });
}

const isQuota = (e) => e && (e.name === 'QuotaExceededError' || e.code === 22);
const bufStart = (sb) => (sb.buffered.length ? sb.buffered.start(0) : Infinity);
const bufEnd = (sb) => (sb.buffered.length ? sb.buffered.end(sb.buffered.length - 1) : -Infinity);
const inBuf = (sb, t) => {
  const b = sb.buffered;
  for (let i = 0; i < b.length; i++) if (b.start(i) <= t && t < b.end(i)) return true;
  return false;
};

class Gapless {
  constructor(video) {
    this.video = video;
    this.ms = null; this.objUrl = null; this.tracks = []; this.dead = false;
    this.ahead = AHEAD;
    this.onerror = null;   // 넣다가 실패하면 부른다 (app.js 가 예전 방식으로 돌아간다)
    this.kick = () => { for (const tr of this.tracks) this.run(tr); };
    this.vidErr = () => this.fail(new Error('재생기 오류'));
  }

  // 영상 없는 입력이면 잘린 소리를 그대로 재생. 영상이 있으면 MediaSource 를 열고 init 을 넣은 뒤 재생 위치 주변을 채운다
  async load({ videoUrl, videoIndex, audioUrl, audioIndex }) {
    const v = this.video;
    if (!videoUrl) { v.src = audioUrl; return; }
    const MS = globalThis.MediaSource;
    if (!MS) throw new Error('MediaSource 없음');
    const vt = `video/mp4; codecs="${videoIndex.codec}"`, at = `audio/mp4; codecs="${audioIndex.codec}"`;
    if (!MS.isTypeSupported(vt) || !MS.isTypeSupported(at)) throw new Error(`지원하지 않는 형식: ${vt}, ${at}`);
    const keeps = audioIndex.keeps || [];
    const total = audioIndex.duration || timeMap(keeps).total;
    const ms = this.ms = new MS();
    this.objUrl = URL.createObjectURL(ms);
    v.src = this.objUrl;
    await new Promise((res) => ms.addEventListener('sourceopen', res, { once: true }));
    if (this.dead) return;
    const track = (type, url, index, pieces, video) =>
      ({ sb: ms.addSourceBuffer(type), url, index, pieces, have: new Array(pieces.length).fill(false), redo: new Array(pieces.length).fill(0), video, busy: false, again: false });
    const vtr = track(vt, videoUrl, videoIndex, planVideo(videoIndex.frags, keeps, videoIndex.frame || 1 / 30), true);
    const atr = track(at, audioUrl, audioIndex, planAudio(audioIndex.frags, total), false);
    await Promise.all([vtr, atr].map(async (tr) => {
      const data = await fetchRange(tr.url, tr.index.init[0], tr.index.init[1] - 1);
      if (!this.dead) await sbWait(tr.sb, () => tr.sb.appendBuffer(data));
    }));
    if (this.dead) return;
    ms.duration = total;   // 기본 컨트롤의 길이 = 결과 길이
    this.tracks = [vtr, atr];
    v.addEventListener('seeking', this.kick);
    v.addEventListener('timeupdate', this.kick);
    v.addEventListener('error', this.vidErr);
    this.kick();
  }

  fail(e) {
    if (this.dead) return;
    console.warn('미리보기 이어 붙이기 실패', e);
    // 비우기(currentTime 이 0 이 됨) 전에 알린다 — 앱이 같은 원본 위치·재생 여부로 예전 방식을 연다
    const cb = this.onerror;
    this.onerror = null;
    try { if (cb) cb(e); } finally { this.destroy(); }
  }

  // 한 트랙을 채우는 반복. 이미 돌고 있으면 한 번 더 돌라고만 표시한다
  async run(tr) {
    if (tr.busy) { tr.again = true; return; }
    tr.busy = true;
    try {
      do {
        tr.again = false;
        while (!this.dead && await this.step(tr)) { /* 할 일이 없을 때까지 */ }
      } while (tr.again && !this.dead);
    } catch (e) {
      tr.busy = false;
      this.fail(e);
      return;
    }
    tr.busy = false;
    this.maybeEnd();
  }

  // 할 일 하나: 지난 것 지우기 → 너무 앞의 것 지우기 → 빠진 조각 넣기. 한 게 있으면 true
  async step(tr) {
    const sb = tr.sb, cur = this.video.currentTime;
    const lo = cur - BEHIND, hi = cur + this.ahead;
    if (bufStart(sb) < lo - 1) { await this.remove(tr, 0, lo); return true; }
    if (bufEnd(sb) > hi + 10) { await this.remove(tr, hi, bufEnd(sb)); return true; }
    const ps = tr.pieces;
    // 브라우저가 한도 때문에 조용히 지운 조각은 다시 넣는다 (아주 짧은 조각은 제 프레임이 없을 수 있어 보지 않고,
    // 넣어도 버퍼에 안 보이는 조각을 끝없이 다시 받지 않게 조각마다 두 번까지만)
    ps.forEach((p, k) => {
      if (tr.have[k] && p.to > cur && p.from < hi && p.to - p.from > 0.1 && isFinite(p.to) && tr.redo[k] < 2 &&
          !inBuf(sb, (p.from + p.to) / 2)) { tr.have[k] = false; tr.redo[k]++; }
    });
    const i = ps.findIndex((p, k) => !tr.have[k] && p.to > cur && p.from < hi);
    if (i < 0) return false;
    let j = i;
    while (j + 1 < ps.length && j + 1 - i < MERGE_MAX && !tr.have[j + 1] && ps[j + 1].keep === ps[i].keep &&
           ps[j + 1].a === ps[j].b + 1 && ps[j + 1].from < hi) j++;
    const data = await fetchRange(tr.url, ps[i].a, ps[j].b);
    if (this.dead) return false;
    if (tr.video) {
      // 창을 먼저 풀어 둔다 (새 시작이 지금 끝보다 크면 오류)
      sb.appendWindowStart = 0; sb.appendWindowEnd = Infinity;
      sb.timestampOffset = ps[i].off;
      sb.appendWindowStart = ps[i].ws; sb.appendWindowEnd = ps[i].we;
    }
    try {
      await sbWait(sb, () => sb.appendBuffer(data));
    } catch (e) {
      if (!isQuota(e)) throw e;
      if (this.ahead > AHEAD_MIN) { this.ahead = AHEAD_MIN; return true; }   // 앞 범위를 줄여 지운 뒤 다시
      return false;   // 이미 줄였으면 재생이 나아가 뒤가 지워질 때(timeupdate) 다시
    }
    for (let k = i; k <= j; k++) tr.have[k] = true;
    // 구간 끝의 +1프레임이 이미 넣은 다음 구간 첫 프레임을 덮었으면 그 조각을 다시 넣는다
    if (tr.video && j + 1 < ps.length && ps[j + 1].keep !== ps[j].keep) tr.have[j + 1] = false;
    return true;
  }

  async remove(tr, a, b) {
    if (!(b > a)) return;
    await sbWait(tr.sb, () => tr.sb.remove(Math.max(0, a), b));
    tr.pieces.forEach((p, k) => { if (p.from < b && p.to > a) tr.have[k] = false; });
  }

  // 지금 위치부터 끝까지 다 넣었으면 endOfStream (중간이 비었는데 부르면 그 자리에서 끝나 버린다)
  maybeEnd() {
    const ms = this.ms;
    if (this.dead || !ms || ms.readyState !== 'open' || !this.tracks.length) return;
    const cur = this.video.currentTime;
    for (const tr of this.tracks) {
      if (tr.busy || tr.sb.updating) return;
      if (tr.pieces.some((p, k) => !tr.have[k] && p.to > cur)) return;
    }
    try { ms.endOfStream(); } catch (e) { /* 다음 기회에 */ }
  }

  destroy() {
    if (this.dead) return;
    this.dead = true;
    this.onerror = null;
    const v = this.video;
    v.removeEventListener('seeking', this.kick);
    v.removeEventListener('timeupdate', this.kick);
    v.removeEventListener('error', this.vidErr);
    v.pause();
    v.removeAttribute('src');
    v.load();
    if (this.objUrl) URL.revokeObjectURL(this.objUrl);
    this.ms = null; this.tracks = [];
  }
}

if (typeof window !== 'undefined') Object.assign(window, { timeMap, fragsFor, planVideo, Gapless });
if (typeof module !== 'undefined') module.exports = { timeMap, fragsFor, planVideo, planAudio, Gapless };
