/* 타임라인 보기 범위 계산 (순수 함수 — 화면 app.js 와 node 테스트가 함께 쓴다).
   보기 범위 view = {t0: 왼쪽 끝 원본 초, spc: 칸 하나의 초}. 칸 너비 cw(px), 줄 너비 w(px). */

// 영상 전체가 줄 너비에 꼭 맞는 보기 (마지막 칸은 잘릴 수 있다)
function fitView(d, w, cw) {
  return { t0: 0, spc: (d * cw) / Math.max(cw, w) };
}

// 칸 하나는 1프레임보다 작게, 전체보다 크게 하지 않고, 왼쪽 끝은 영상 안에서
function clampView(v, d, w, cw, fps) {
  const fit = fitView(d, w, cw).spc;
  const spc = Math.min(Math.max(v.spc, 1 / fps), fit);
  const t0 = Math.min(Math.max(0, v.t0), Math.max(0, d - (spc * w) / cw));
  return { t0, spc };
}

const tToX = (v, t, cw) => ((t - v.t0) / v.spc) * cw;
const xToT = (v, x, cw) => v.t0 + (x / cw) * v.spc;

// factor 배 확대 (1보다 작으면 축소). anchorT 는 화면에서 같은 자리에 남는다
function zoomAt(v, factor, anchorT, d, w, cw, fps) {
  const x = tToX(v, anchorT, cw);
  const spc = clampView({ t0: 0, spc: v.spc / factor }, d, w, cw, fps).spc;   // 한계에서 멈춘 배율로 자리를 잡는다
  return clampView({ t0: anchorT - (x / cw) * spc, spc }, d, w, cw, fps);
}

// t 가 보이면 그대로, 아니면 t 가 왼쪽에서 10% 자리에 오게 넘긴다
function followT(v, t, d, w, cw, fps) {
  const span = (v.spc * w) / cw;
  if (t >= v.t0 && t < v.t0 + span) return v;
  return clampView({ t0: t - 0.1 * span, spc: v.spc }, d, w, cw, fps);
}

// 재생 중 따라가기: 재생 위치가 가운데를 넘거나 보기 밖이면 가운데에 두고 보기가 같이 흐른다 (쪽 넘김 없이)
function playFollow(v, t, d, w, cw, fps) {
  const span = (v.spc * w) / cw;
  if (t >= v.t0 && t <= v.t0 + span / 2) return v;
  return clampView({ t0: t - span / 2, spc: v.spc }, d, w, cw, fps);
}

// 재생 중 칸 하나의 초: 줄이 초당 pxPerSec 보다 빨리 흐르지 않게 (1칸 = 1프레임이면 너무 빨라 알아볼 수 없다)
function playSpc(spc, cw, pxPerSec) {
  return Math.max(spc, cw / pxPerSec);
}

// 보이는 칸들: 칸 시작 시간 t (spc 격자), 화면 x
function cells(v, d, w, cw) {
  const out = [];
  const n0 = Math.floor(v.t0 / v.spc + 1e-9);
  for (let k = 0; ; k++) {
    const t = (n0 + k) * v.spc;
    const x = tToX(v, t, cw);
    if (x >= w || t >= d) break;
    out.push({ i: n0 + k, t, x });
  }
  return out;
}

// 시간 t 의 썸네일이 시트 몇 번의 몇째 칸인지 (get_thumbs 의 배치)
function sheetCell(t, meta) {
  const n = Math.floor(t / meta.step + 1e-9);
  if (n < 0 || n >= meta.count) return null;
  const k = n % meta.per_sheet;
  return { sheet: Math.floor(n / meta.per_sheet), col: k % meta.cols, row: Math.floor(k / meta.cols) };
}

// [a, b) 구간 파형 값(0~255)의 최대. 한 값보다 좁으면 그 자리 값
function waveMax(bytes, hop, a, b) {
  const i0 = Math.max(0, Math.floor(a / hop + 1e-9));
  const i1 = Math.min(bytes.length, Math.max(i0 + 1, Math.ceil(b / hop - 1e-9)));
  let m = 0;
  for (let i = i0; i < i1; i++) if (bytes[i] > m) m = bytes[i];
  return m;
}

// 확대 슬라이더 0~1000 (0 = 가장 축소 maxSpc, 1000 = 가장 확대 minSpc), 로그 눈금
function spcToSlider(spc, minSpc, maxSpc) {
  if (!(maxSpc > minSpc) || spc >= maxSpc) return 0;
  if (spc <= minSpc * (1 + 1e-9)) return 1000;   // 양 끝은 정확히 (부동소수 오차로 999.99… 가 되지 않게)
  return (1000 * Math.log(maxSpc / spc)) / Math.log(maxSpc / minSpc);
}
function sliderToSpc(s, minSpc, maxSpc) {
  if (!(maxSpc > minSpc)) return maxSpc;
  return maxSpc * Math.pow(minSpc / maxSpc, s / 1000);
}

// ---- 이미지 겹쳐 넣기 ----
// 이미지 o = {x, y, w, h?, aspect}: x·y(왼쪽 위)·w 는 영상 화면 비율, h 는 영상 높이 대비(없으면 그림 비율대로),
// aspect = 그림 너비/높이. box = 화면에 그려진 영상 자리(px)
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
const OV_MIN_W = 0.02;
const ovH = (o, box) => (o.h != null ? o.h : (o.w * box.w) / o.aspect / box.h);   // 높이 (영상 높이 대비)

function ovRect(o, box) {
  return { x: box.x + o.x * box.w, y: box.y + o.y * box.h, w: o.w * box.w, h: ovH(o, box) * box.h };
}
// px 만큼 옮긴 자리 (영상 밖으로 나가지 않게)
function ovMove(o, dx, dy, box) {
  return { x: clamp(o.x + dx / box.w, 0, 1 - o.w), y: clamp(o.y + dy / box.h, 0, 1 - ovH(o, box)) };
}
// 손잡이 끌기: edge 는 'n','s','e','w' 또는 모서리 'ne','nw','se','sw'. 끄는 변만 움직이고 반대쪽은 그대로.
// keep(Shift)이면 모서리는 지금 가로세로 비율을 유지 (가로 기준). 영상 안, 최소 OV_MIN_W. → {x, y, w, h} 비율
function ovResizeEdge(o, edge, dx, dy, box, keep) {
  const minW = OV_MIN_W * box.w, minH = OV_MIN_W * box.h;
  let l = o.x * box.w, t = o.y * box.h, r = l + o.w * box.w, b = t + ovH(o, box) * box.h;
  const ratio = (r - l) / (b - t);
  if (edge.includes('w')) l = clamp(l + dx, 0, r - minW);
  if (edge.includes('e')) r = clamp(r + dx, l + minW, box.w);
  if (edge.includes('n')) t = clamp(t + dy, 0, b - minH);
  if (edge.includes('s')) b = clamp(b + dy, t + minH, box.h);
  if (keep && edge.length === 2) {
    // 가로를 기준으로 세로를 맞추되, 세로가 영상 밖으로 나가면 가로를 줄인다
    const room = edge.includes('n') ? b : box.h - t;
    const w = Math.min(r - l, room * ratio);
    const h = w / ratio;
    if (edge.includes('w')) l = r - w; else r = l + w;
    if (edge.includes('n')) t = b - h; else b = t + h;
  }
  return { x: l / box.w, y: t / box.h, w: (r - l) / box.w, h: (b - t) / box.h };
}
// 타임라인 막대 끌기: kind 'move' | 'start' | 'end', dt 초. 프레임 격자, [0, d] 안, 최소 1프레임
function barDrag(kind, s, e, dt, d, fps) {
  const snap = (t) => Math.round(t * fps) / fps;
  const one = 1 / fps;
  if (kind === 'move') {
    const start = clamp(snap(s + dt), 0, d - (e - s));
    return { start, end: start + (e - s) };
  }
  if (kind === 'start') return { start: clamp(snap(s + dt), 0, e - one), end: e };
  return { start: s, end: clamp(snap(e + dt), s + one, d) };
}

// 이미지 줄 영역 높이 끌기: 시작 높이 h0(px)에서 dy(아래로 +)만큼 끌었을 때 새 높이.
// 한 줄(lane)과 내용 높이(content) 사이, 영상(시작 때 vid0)이 minVid 보다 작아지게 키우지는 않는다
function lanesDrag(h0, dy, content, vid0, minVid, lane) {
  const top = Math.min(content, Math.max(lane, h0 + Math.max(0, vid0 - minVid)));
  return clamp(h0 - dy, Math.min(lane, content), top);
}

// ---- 영상 끼워 넣기 ----
// 끼워 넣은 조각 [{clip, start, end}](이어 붙인 초)의 화면 띠 [{clip, x0, x1}]: 줄 [0, w] 안으로 자르고 안 보이는 것은 뺀다
function clipBands(pieces, v, w, cw) {
  const out = [];
  for (const p of pieces) {
    const x0 = Math.max(0, tToX(v, p.start, cw)), x1 = Math.min(w, tToX(v, p.end, cw));
    if (x1 > x0) out.push({ clip: p.clip, x0, x1 });
  }
  return out;
}

// ---- 영상 옮기기 자리 맞춤 (app/sequence.py 의 snap_at 과 같은 규칙) ----
// t 를 프레임 격자 → [0, total] 에 맞추고, 잘린 구간 [a, b] 안이면 그 안의 영상 경계 중 가장 가까운 것(같으면 앞쪽), 없으면 a 를 격자에
function snapAt(t, cuts, bounds, fps, total) {
  const grid = (x) => clamp(Math.round(x * fps) / fps, 0, total);
  const e = 1e-6;
  t = grid(t);
  const merged = [];   // 겹치거나 맞닿은 컷은 합침 (app/api.py _union 과 같게)
  for (const [a, b] of [...cuts].sort((p, q) => p[0] - q[0])) {
    if (merged.length && a <= merged[merged.length - 1][1]) merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], b);
    else merged.push([a, b]);
  }
  for (const [a, b] of merged) {
    if (a + e < t && t < b - e) {
      let best = null;
      for (const x of [...bounds].sort((p, q) => p - q)) {
        if (a + e < x && x < b - e && (best === null || Math.abs(x - t) < Math.abs(best - t))) best = x;
      }
      return best === null ? grid(a) : best;
    }
  }
  return t;
}
// 덩어리 block [s, e] 를 t 로 끌어 놓을 자리. 덩어리 안이거나 s·e 와 같으면(제자리) null
function dropTarget(t, cuts, bounds, fps, total, block) {
  const x = snapAt(t, cuts, bounds, fps, total), e = 1e-6;
  return x > block[0] - e && x < block[1] + e ? null : x;
}
// 누른 채 4px 넘게 움직였으면 끌기
function isDrag(dx, dy) { return Math.hypot(dx, dy) > 4; }

// 썸네일·소리 줄을 다시 읽을 키: 작업이 같아도 길이나 조각 자리(get_result 의 layout)가 바뀌면 asr.wav·미리보기 영상이 바뀐다
function tlDataKey(id, r) {
  return JSON.stringify([id, (r && r.duration) || 0, (r && r.layout) || []]);
}

// ---- 음원(BGM) 줄 (app/bgm.py 와 같은 규칙) ----
// map = player.js 의 timeMap(keeps). 음원 b = {at(결과 초), in, out(곡 초), dur}, points = [[곡 초, dB], …]
const BGM_DB_MAX = 6, BGM_DB_MIN = -40, BGM_MIN_LEN = 0.1;
const bgmLen = (b) => b.out - b.in;

// 곡 초 t 의 dB: 점 사이는 직선, 첫 점 앞·마지막 점 뒤는 그 점 값, 점이 없으면 0 (bgm.db_at)
function dbAt(points, t) {
  if (!points || !points.length) return 0;
  return dbAtSorted(points.map(([a, b]) => [+a, +b]).sort((p, q) => p[0] - q[0]), t);
}
function dbAtSorted(pts, t) {
  if (!pts.length) return 0;
  if (t <= pts[0][0]) return pts[0][1];
  const last = pts[pts.length - 1];
  if (t >= last[0]) return last[1];
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
    if (t <= x1) return x1 === x0 ? y1 : y0 + ((y1 - y0) * (t - x0)) / (x1 - x0);
  }
  return last[1];
}

// 줄 높이 h: 위 +6dB, 아래 -40dB 의 dB 직선 축
const dbToY = (db, h) => ((BGM_DB_MAX - db) / (BGM_DB_MAX - BGM_DB_MIN)) * h;
const yToDb = (y, h) => clamp(BGM_DB_MAX - (y / h) * (BGM_DB_MAX - BGM_DB_MIN), BGM_DB_MIN, BGM_DB_MAX);

// 겹치지 않는 가장 위 줄 번호 {id: lane}: at 순(같으면 id 순), 겹침은 결과 구간 [at, at+길이)
function bgmLanes(bgms) {
  const order = [...bgms].sort((a, b) => a.at - b.at || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const ends = [], out = {};
  for (const b of order) {
    let k = ends.findIndex((e) => e <= b.at + 1e-9);
    if (k < 0) { k = ends.length; ends.push(0); }
    ends[k] = b.at + bgmLen(b);
    out[b.id] = k;
  }
  return out;
}

// 원본 축 구간 {o0, o1, cut}: 결과 끝을 넘으면 cut(넘침 표시). 시작이 결과 끝 이후면 null
function bgmSpan(b, map) {
  const total = map.total, end = b.at + bgmLen(b);
  if (b.at >= total) return null;
  return { o0: map.toOrig(b.at), o1: map.toOrig(Math.min(end, total)), cut: end > total };
}

// 곡선의 화면 꺾은선 [[x, y], …]: 결과 시간을 map.pieces(남길 구간)별로 원본 축에 펼치고, 잘린 구간 위는 앞 구간 끝 값으로 평평하게.
// 보기 v(원본 초, 줄 너비 w px — 없으면 오른쪽 제한 없음)와 겹치는 구간만 내놓고, 보기 밖 앞 구간은 연결선 시작점 하나만 둔다
function bgmPolyline(b, points, map, v, cw, h, w = Infinity) {
  const pieces = map.pieces, rEnd = Math.min(b.at + bgmLen(b), map.total);
  const pts = (points || []).map(([a, c]) => [+a, +c]).sort((p, q) => p[0] - q[0]);
  const vEnd = v.t0 + (v.spc * w) / cw;
  const y = (r) => dbToY(dbAtSorted(pts, b.in + (r - b.at)), h);
  const out = [];
  let lo = 0, hi = pieces.length;   // b.at 이 속한 첫 구간
  while (lo < hi) { const m = (lo + hi) >> 1; if (pieces[m].o + (pieces[m].e - pieces[m].s) > b.at) hi = m; else lo = m + 1; }
  let j = 0, prevO = null, prevY = 0;
  for (let i = lo; i < pieces.length; i++) {
    const k = pieces[i], ra = Math.max(b.at, k.o), rb = Math.min(rEnd, k.o + (k.e - k.s));
    if (ra >= rb) break;
    const oa = k.s + (ra - k.o), ob = k.s + (rb - k.o);
    if (oa > vEnd) {   // 보기 오른쪽 밖: 앞 구간 끝 값으로 이어지는 연결선만 보기 끝 너머까지
      if (prevO !== null) out.push([tToX(v, oa, cw), prevY]);
      break;
    }
    if (ob >= v.t0) {
      if (prevO !== null) {
        if (!out.length) out.push([tToX(v, prevO, cw), prevY]);
        if (oa > prevO + 1e-9) out.push([tToX(v, oa, cw), prevY]);
      }
      out.push([tToX(v, oa, cw), y(ra)]);
      while (j < pts.length && b.at + (pts[j][0] - b.in) <= ra + 1e-9) j++;
      for (; j < pts.length; j++) {
        const pr = b.at + (pts[j][0] - b.in);
        if (pr >= rb - 1e-9) break;
        out.push([tToX(v, k.s + (pr - k.o), cw), dbToY(pts[j][1], h)]);
      }
      out.push([tToX(v, ob, cw), y(rb)]);
    }
    prevO = ob; prevY = y(rb);
  }
  return out;
}

// 끌어 옮길 때 자리 맞춤: 시작/끝 중 하나가 대상(시작은 0, 끝은 결과 끝, 다른 음원 경계)과 thr px 안이면 붙임(가장 가까운 것)
function bgmSnap(at, len, total, others, pxPerSec, thr = 8) {
  let best = at, bestD = Infinity;
  const tryAt = (cand, d) => { if (d * pxPerSec <= thr && d < bestD) { best = cand; bestD = d; } };
  tryAt(0, Math.abs(at));
  tryAt(total - len, Math.abs(at + len - total));
  for (const t of others) { tryAt(t, Math.abs(at - t)); tryAt(t - len, Math.abs(at + len - t)); }
  return Math.max(0, best);
}

// 음원 끌기 {at, in, out}: move 는 자리만, start/end 는 곡 안쪽 가장자리 (start 는 at 도 같은 양 움직여 곡이 제자리에 남음)
function bgmDrag(kind, b, dt, total) {
  // move: 결과 끝 뒤로 빠지면 막대가 안 보여 다시 잡을 수 없다 — 끝에서 MIN_LEN 은 남긴다
  if (kind === 'move') return { at: clamp(b.at + dt, 0, Math.max(0, total - BGM_MIN_LEN)), in: b.in, out: b.out };
  if (kind === 'start') {
    const nin = Math.max(clamp(b.in + dt, 0, b.out - BGM_MIN_LEN), b.in - b.at);   // at >= 0 이 되게
    return { at: b.at + (nin - b.in), in: nin, out: b.out };
  }
  return { at: b.at, in: b.in, out: clamp(b.out + dt, b.in + BGM_MIN_LEN, b.dur) };
}

// 화면 x → 원본 초 → 결과 초 (잘린 구간 안이면 그 구간 끝의 결과 시간)
const xToOutT = (x, map, v, cw) => map.toOut(xToT(v, x, cw));

const TL = { fitView, clampView, tToX, xToT, zoomAt, followT, playFollow, playSpc, cells, sheetCell, waveMax, spcToSlider, sliderToSpc,
  ovRect, ovMove, ovResizeEdge, barDrag, lanesDrag, clipBands, snapAt, dropTarget, isDrag, tlDataKey,
  dbAt, dbAtSorted, dbToY, yToDb, bgmLanes, bgmSpan, bgmPolyline, bgmSnap, bgmDrag, xToOutT };
if (typeof window !== 'undefined') window.TL = TL;
if (typeof module !== 'undefined') module.exports = TL;
